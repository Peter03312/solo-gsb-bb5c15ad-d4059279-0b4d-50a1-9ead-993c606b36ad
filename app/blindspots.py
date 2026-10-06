"""质控：测温盲区查询（纯逻辑 + 一次上下文加载的查询装配）。

“测温盲区”指查询窗内**相邻有效测温读数之间（或查询边界与相邻读数之间）
严格长于最大允许无有效测温时长**的间隔。质控人员按货物编号、带时区的查询
起止时刻与阈值查看，用于发现长时间没有任何可信测温覆盖的运输 / 仓储时段。

有效读数（覆盖）口径
--------------------
仅取**查询窗内**（左闭右开 ``[query_start, query_end)``）下列两类读数：

* 该货物的**自有测温**事件；
* 读数时刻落在该货物**当前装卸窗口**内（左闭右开
  ``loaded_at <= read_at < unloaded_at``）、且**未被生效探头故障隔离**的
  车载读数；同刻存在货物自有测温时车载读数被取代（``adopted=false``），
  不计入覆盖。

合并时间线（见 :mod:`app.timeline`）已经统一了上述适用性、同刻自有优先与
故障隔离判定，故本模块只消费其**被采用（``adopted=true``）的测温点**；
测温点是否越温、温度是否经更正都不影响覆盖（交接点不是测温，也不覆盖）。

盲区划分
--------
以查询边界与窗内相邻有效读数时刻作为切分点，把查询窗划分为若干相邻间隔：

* 间隔由两端的切分点（查询边界或有效读数时刻）界定；
* 间隔长度**严格长于**阈值（``duration > max_gap``）才列为盲区，等于不算；
* 无任何窗内有效读数时，整段查询窗作为同一个间隔按同一规则判定。

盲区两端读数来源：间隔左端读数为查询起点时（边界、无读数）来源为空；
右端读数为查询终点时同理为空。

保管方片段
----------
盲区再按货物自有**交接时刻**切分为保管方片段：交接点归接收方
（``[片段起, 交接时刻)`` 归交出方，``[交接时刻, 片段止)`` 归接收方），
不产生零长度片段。保管方只由货物自有交接改变（车载读数不改变保管方）。

晚到读数、装卸窗口更正与探头故障撤销后均按**当前状态**重建（本查询只读，
不改任何原记录）；测温值更正只改温度、不改变读数时刻，故不改变覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ReadingRef:
    """盲区一端的有效测温读数坐标（边界端为 None）。"""

    at: datetime
    source: str  # own=货物自有测温；vehicle=车载读数
    event_id: str | None = None    # source=own 时
    reading_id: str | None = None  # source=vehicle 时
    trip_id: str | None = None     # source=vehicle 时


@dataclass(frozen=True)
class CustodyPiece:
    """盲区按交接时刻切出的一个保管方片段（左闭右开，交接点归接收方）。"""

    start: datetime
    end: datetime
    responsible_party: str

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class BlindSpot:
    """一段测温盲区：[start, end) 内没有任何有效测温读数。"""

    start: datetime
    end: datetime
    # 两端读数坐标：该端为查询边界（无相邻读数）时为 None
    start_reading: ReadingRef | None
    end_reading: ReadingRef | None
    custody_pieces: tuple[CustodyPiece, ...]

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def _reading_ref(point) -> ReadingRef:
    """合并时间线上一个被采用测温点 -> 读数坐标。"""
    return ReadingRef(
        at=point.occurred_at,
        source=point.source,
        event_id=point.source_id if point.source == "own" else None,
        reading_id=point.source_id if point.source == "vehicle" else None,
        trip_id=point.trip_id,
    )


def custody_pieces(
    start: datetime,
    end: datetime,
    handovers,
    initial_holder: str,
) -> tuple[CustodyPiece, ...]:
    """把 [start, end) 按自有交接时刻切分为保管方片段。

    ``handovers`` 为按时间升序的交接时间线点（``TimelinePoint``，携带
    ``occurred_at`` / ``receiver``），可包含区间外的交接：

    * 早于 / 等于 ``start`` 的交接只用于把保管方推进到起点时的现任方
      （恰在起点的交接归接收方）；
    * ``(start, end)`` 内的交接切分：``[片段起, 交接时刻)`` 归交出方，
      自交接时刻起归接收方；
    * 恰在 ``end`` 的交接不影响本区间（右端不含），末片段仍归交出方；
    * 不产生零长度片段。
    """
    holder = initial_holder
    seg_start = start
    pieces: list[CustodyPiece] = []
    for h in handovers:
        at = h.occurred_at
        if at <= start:
            # 起点之前（含恰在起点）的交接：起点时的现任保管方
            holder = h.receiver
            continue
        if at >= end:
            break
        # start < at < end：at 必严格晚于当前片段起点，片段恒为正长度
        pieces.append(CustodyPiece(seg_start, at, holder))
        holder = h.receiver
        seg_start = at
    pieces.append(CustodyPiece(seg_start, end, holder))
    return tuple(pieces)


def find_blind_spots(
    adopted_points,
    win_start: datetime,
    win_end: datetime,
    max_gap: timedelta,
    initial_holder: str,
) -> tuple[BlindSpot, ...]:
    """在已采用时间线上计算查询窗内的测温盲区（按开始时刻升序）。

    :param adopted_points: 货物合并时间线中**被采用**的点
        （:func:`app.timeline.adopted_timeline` 的结果，含自有 / 车载测温
        与自有交接；被同刻自有取代、被生效故障隔离的车载点不在其中）。
    :param win_start / win_end: 查询窗，左闭右开，``win_start < win_end``。
    :param max_gap: 最大允许无有效测温时长（正时间差）；间隔严格长于它才为盲区。
    :param initial_holder: 货物起始保管方，用于交接片段重建。
    """
    if win_end <= win_start:
        raise ValueError("查询窗必须左闭右开且起点严格早于终点")
    if max_gap <= timedelta(0):
        raise ValueError("最大允许无有效测温时长必须为正")

    measurements = [p for p in adopted_points if p.is_measurement]
    handovers = [p for p in adopted_points if p.is_handover]

    # 窗内有效读数时刻（左闭右开）；被采用测温点同刻至多一个，按时刻去重
    inside: dict[datetime, object] = {}
    for p in measurements:
        if win_start <= p.occurred_at < win_end:
            inside.setdefault(p.occurred_at, p)
    ordered = sorted(inside.values(), key=lambda p: p.occurred_at)

    # 切分点：查询边界 + 窗内有效读数时刻
    anchors: list[datetime] = [win_start]
    anchors.extend(p.occurred_at for p in ordered)
    anchors.append(win_end)

    ref_by_at = {p.occurred_at: _reading_ref(p) for p in ordered}

    blind_spots: list[BlindSpot] = []
    for left, right in zip(anchors, anchors[1:]):
        if right - left <= max_gap:
            continue
        start_ref = ref_by_at.get(left)   # left==win_start 时为 None
        end_ref = ref_by_at.get(right)   # right==win_end 时为 None
        pieces = custody_pieces(left, right, handovers, initial_holder)
        blind_spots.append(
            BlindSpot(
                start=left,
                end=right,
                start_reading=start_ref,
                end_reading=end_ref,
                custody_pieces=pieces,
            )
        )
    return tuple(blind_spots)


def query_cargo_blind_spots(db, cargo_id: str, start_at, end_at, max_gap: timedelta):
    """测温盲区查询的数据库装配：一次加载货物上下文并在其上重建。

    只读查询，整次查询基于一个**一致的已提交数据视图**：PostgreSQL 下以
    REPEATABLE READ 快照事务执行（与同时越温查询一致；SQLite 单连接读本身
    串行）。晚到读数 / 事件在本次查询事务开始前已提交即被反映；测温值更正
    只改温度、不改变覆盖；装卸窗口更正、故障撤销后按当前状态重建。

    返回 ``(cargo, blind_spots)``；货物不存在抛 404 ``cargo_not_found``。
    """
    from app import timeline
    from app.crud import ApiError
    from app.models import Cargo

    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        # 快照在首条 SQL 时建立；必须先于任何查询设置
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")

    events, loadings, readings, effective, quarantine, standards = (
        timeline.load_cargo_context(db, cargo_id)
    )
    adopted = timeline.adopted_timeline(
        events,
        readings,
        loadings,
        effective[0],
        effective[1],
        quarantine,
        standards,
    )

    blind_spots = find_blind_spots(
        adopted, start_at, end_at, max_gap, cargo.initial_holder
    )
    return cargo, blind_spots
