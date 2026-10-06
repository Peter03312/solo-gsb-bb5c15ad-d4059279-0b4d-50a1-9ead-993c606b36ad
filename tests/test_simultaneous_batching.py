"""同时越温查询：分批数据装配（本轮重构）。

覆盖：
* ``timeline.load_cargo_contexts_batch`` 与逐票 ``load_cargo_context``
  产出的重建上下文等价（跨车次历史、有效温度、故障隔离、分时标准）；
* 车载读数只装载对批次内货物在车的行：无关车次 / 从不在车时刻的读数
  不加载；``read_before`` 裁掉查询终点之后的点；
* 数据库往返按**批次**而非票数增长（``SIMULTANEOUS_CARGO_BATCH_SIZE``）；
* 无装卸 / 车上货物不达门槛时直接空结果，不装配每票上下文。
"""
from decimal import Decimal

from sqlalchemy import event

from app.database import SessionLocal, engine
from app import chain, timeline


# ------------------------------------------------------------ 装配辅助


def _make_cargo(client, cargo_id, limit="4", started_at="2026-01-01T00:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": started_at,
            "temp_upper_limit": limit,
            "initial_holder": "A",
        },
    )
    assert r.status_code == 201, r.text


def _manifest(client, trip, items):
    r = client.post(f"/trips/{trip}/manifest", json={"items": items})
    assert r.status_code == 201, r.text


def _vr(client, rid, trip, at, temp):
    r = client.post(
        "/vehicle-readings",
        json={"reading_id": rid, "trip_id": trip, "read_at": at,
              "temperature": str(temp)},
    )
    assert r.status_code in (200, 201), r.text


def _own(client, eid, cargo, at, temp=None, giver=None, receiver=None):
    body = {"event_id": eid, "cargo_id": cargo, "occurred_at": at}
    if temp is not None:
        body["temperature"] = str(temp)
    else:
        body["giver"], body["receiver"] = giver, receiver
    r = client.post("/events", json=body)
    assert r.status_code == 201, r.text


def _query(client, trip, start="2026-01-01T00:00:00+00:00",
           end="2026-01-02T00:00:00+00:00", **extra):
    params = {"start_at": start, "end_at": end, **extra}
    return client.get(f"/trips/{trip}/simultaneous-excursions", params=params)


class _StatementCounter:
    """统计作用域内发往引擎的 SQL 语句数。"""

    def __init__(self):
        self.count = 0

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._tick)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._tick)

    def _tick(self, *args, **kwargs):
        self.count += 1


def _adopted_from_context(ctx):
    events, loadings, readings, effective, quarantine, standards = ctx
    return timeline.adopted_timeline(
        events,
        readings,
        loadings,
        effective[0],
        effective[1],
        quarantine,
        standards,
    )


# ------------------------------------------------------------ 批量上下文等价性


def test_batch_context_matches_per_cargo_context_across_trips(client):
    # 三票货物：T1 / T2 两个车次，含跨车次开放区间、更正、故障、分时标准。
    for cid, limit in (("B1", "4"), ("B2", "5"), ("B3", "4")):
        _make_cargo(client, cid, limit)
    for trip in ("T1", "T2"):
        assert client.post(
            "/trips", json={"trip_id": trip, "vehicle_no": "v"}
        ).status_code == 201
    _manifest(client, "T1", [
        {"cargo_id": c,
         "loaded_at": "2026-01-01T06:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"}
        for c in ("B1", "B2")
    ])
    _manifest(client, "T2", [
        {"cargo_id": "B1",
         "loaded_at": "2026-01-01T12:00:00+00:00",
         "unloaded_at": "2026-01-01T20:00:00+00:00"},
        {"cargo_id": "B3",
         "loaded_at": "2026-01-01T13:00:00+00:00",
         "unloaded_at": "2026-01-01T19:00:00+00:00"},
    ])
    _vr(client, "vr-hot-1", "T1", "2026-01-01T08:00:00+00:00", 9)
    _vr(client, "vr-hot-2", "T2", "2026-01-01T14:00:00+00:00", 9)
    # B1 在 T1 同刻自有测温 -> 取代车载
    _own(client, "e-b1", "B1", "2026-01-01T08:00:00+00:00", 2)
    # T2 探头故障覆盖 14:00 读数（对 B1、B3 隔离）
    assert client.post(
        "/trips/T2/probe-faults",
        json={
            "fault_id": "f1",
            "fault_start": "2026-01-01T13:00:00+00:00",
            "fault_end": "2026-01-01T18:00:00+00:00",
            "reason": "探头失准",
        },
    ).status_code in (200, 201)
    # B2 分时上限变更（10:00 起收紧到 3）
    assert client.post(
        "/cargo/B2/temp-standards",
        json={
            "change_id": "std-b2",
            "effective_at": "2026-01-01T10:00:00+00:00",
            "new_upper_limit": "3.0",
            "expected_version": 0,
            "reason": "收紧",
        },
    ).status_code in (200, 201)
    # 车载读数更正（T1 的 08:00 读数 9 -> 1）
    assert client.post(
        "/corrections",
        json={
            "correction_id": "corr-1",
            "target_type": "vehicle_reading",
            "target_id": "vr-hot-1",
            "expected_version": 0,
            "new_temperature": "1.0",
        },
    ).status_code in (200, 201)

    db = SessionLocal()
    try:
        batch = timeline.load_cargo_contexts_batch(db, ["B1", "B2", "B3"])
        for cid in ("B1", "B2", "B3"):
            single = timeline.load_cargo_context(db, cid)
            bp = _adopted_from_context(batch[cid])
            sp = _adopted_from_context(single)
            # 被采用点的（时刻, 来源, 来源ID, 温度, 版本, 当时上限）逐项一致
            def _sig(points):
                return [
                    (
                        p.occurred_at,
                        p.source,
                        p.source_id,
                        p.trip_id,
                        Decimal(p.temperature),
                        p.temp_version,
                        p.temp_limit,
                    )
                    for p in points
                ]

            assert _sig(bp) == _sig(sp), cid
            # 重建出的越温区间（起, 止, 来源）一致
            from app.models import Cargo

            cargo = db.get(Cargo, cid)
            rb = chain.rebuild_chain(
                cargo.initial_holder, Decimal(cargo.temp_upper_limit), bp
            )
            rs = chain.rebuild_chain(
                cargo.initial_holder, Decimal(cargo.temp_upper_limit), sp
            )
            assert [
                (e.start, e.end, e.start_source, e.start_reading_id)
                for e in rb.excursions
            ] == [
                (e.start, e.end, e.start_source, e.start_reading_id)
                for e in rs.excursions
            ], cid
    finally:
        db.close()


def test_batch_does_not_load_unrelated_or_offboard_or_future_readings(client):
    _make_cargo(client, "D1")
    _make_cargo(client, "D2")
    for trip in ("R1", "R2", "R3"):
        assert client.post(
            "/trips", json={"trip_id": trip, "vehicle_no": "v"}
        ).status_code == 201
    # D1 只在 R1；D2 在 R2
    _manifest(client, "R1", [
        {"cargo_id": "D1",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    _manifest(client, "R2", [
        {"cargo_id": "D2",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T12:00:00+00:00"},
    ])
    _vr(client, "on-r1", "R1", "2026-01-01T09:00:00+00:00", 9)   # D1 在车
    _vr(client, "off-r1", "R1", "2026-01-01T13:00:00+00:00", 9)  # D1 已下车
    _vr(client, "on-r2", "R2", "2026-01-01T09:00:00+00:00", 9)   # D2 在车
    _vr(client, "on-r3", "R3", "2026-01-01T09:00:00+00:00", 9)   # 无票在车上

    db = SessionLocal()
    try:
        from datetime import datetime, timezone

        end = datetime(2026, 1, 2, tzinfo=timezone.utc)
        # 一笔查询终点之后的读数（D1 在车上，但时刻 >= end）
        _vr_local_id = "future-r1"
        assert client.post(
            "/vehicle-readings",
            json={"reading_id": _vr_local_id, "trip_id": "R1",
                  "read_at": "2026-01-02T09:00:00+00:00",
                  "temperature": "9"},
        ).status_code in (200, 201)

        ctx = timeline.load_cargo_contexts_batch(db, ["D1", "D2"], read_before=end)
        loaded_ids = {
            r.reading_id
            for _, _, readings, _, _, _ in ctx.values()
            for r in readings
        }
        # 仅装载对批次内某票在车且早于终点的读数
        assert loaded_ids == {"on-r1", "on-r2"}
    finally:
        db.close()


# ------------------------------------------------------------ 往返按批次增长


def test_round_trips_grow_with_batches_not_tickets(client, monkeypatch):
    n = 6
    for i in range(1, n + 1):
        _make_cargo(client, f"P{i}")
    assert client.post(
        "/trips", json={"trip_id": "BIG", "vehicle_no": "v"}
    ).status_code == 201
    _manifest(client, "BIG", [
        {"cargo_id": f"P{i}",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"}
        for i in range(1, n + 1)
    ])
    _vr(client, "vr-1", "BIG", "2026-01-01T09:00:00+00:00", 9)
    _vr(client, "vr-2", "BIG", "2026-01-01T10:00:00+00:00", 3)

    counts = {}
    for size in (1, 2, 3, 6):
        monkeypatch.setenv("SIMULTANEOUS_CARGO_BATCH_SIZE", str(size))
        with _StatementCounter() as sc:
            r = _query(client, "BIG")
        assert r.status_code == 200, r.text
        counts[size] = sc.count

    # 6 票 / 每批固定条数集合查询：批次越多往返越多；单批装下时往返最少。
    assert counts[1] > counts[2] > counts[3] >= counts[6]
    # 整批（6 票一批）与每票一批相比，往返显著更少（逐票装配为 6 倍量级）
    assert counts[6] * 2 <= counts[1]
    # 结果不受批大小影响
    monkeypatch.setenv("SIMULTANEOUS_CARGO_BATCH_SIZE", "2")
    segs = _query(client, "BIG").json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"),
    ]
    assert segs[0]["cargo_count"] == 6


def test_batch_loader_single_call_fixed_statements_per_batch(client):
    # 同一批内票数翻倍（3 -> 6，均在一批内装下）不增加装配往返。
    def _setup(n):
        trip = f"T{n}"
        for i in range(1, n + 1):
            _make_cargo(client, f"Q{n}-{i}")
        assert client.post(
            "/trips", json={"trip_id": trip, "vehicle_no": "v"}
        ).status_code == 201
        _manifest(client, trip, [
            {"cargo_id": f"Q{n}-{i}",
             "loaded_at": "2026-01-01T08:00:00+00:00",
             "unloaded_at": "2026-01-01T18:00:00+00:00"}
            for i in range(1, n + 1)
        ])
        _vr(client, f"vr-{n}", trip, "2026-01-01T09:00:00+00:00", 9)
        return [f"Q{n}-{i}" for i in range(1, n + 1)]

    ids3 = _setup(3)
    ids6 = _setup(6)
    db = SessionLocal()
    try:
        with _StatementCounter() as sc3:
            timeline.load_cargo_contexts_batch(db, ids3)
        with _StatementCounter() as sc6:
            timeline.load_cargo_contexts_batch(db, ids6)
        # 两批均为单批装载：语句数相同（货物 / 事件 / 装卸 / 读数 /
        # 故障 / 自有更正 / 车载更正 / 标准各一条集合查询，与票数无关）
        assert sc3.count == sc6.count
    finally:
        db.close()


# ------------------------------------------------------------ 早退：不装配上下文


def test_no_manifest_returns_empty_without_context_load(client):
    assert client.post(
        "/trips", json={"trip_id": "EMPTY", "vehicle_no": "v"}
    ).status_code == 201
    with _StatementCounter() as sc:
        body = _query(client, "EMPTY").json()
    assert body["segments"] == []
    # 仅车次存在性 + 装卸两条查询，无任何每票上下文查询
    assert sc.count <= 2


def test_cargo_count_below_threshold_short_circuits(client):
    _make_cargo(client, "SOLO")
    assert client.post(
        "/trips", json={"trip_id": "SOLO-T", "vehicle_no": "v"}
    ).status_code == 201
    _manifest(client, "SOLO-T", [
        {"cargo_id": "SOLO",
         "loaded_at": "2026-01-01T08:00:00+00:00",
         "unloaded_at": "2026-01-01T18:00:00+00:00"},
    ])
    _vr(client, "vr-solo", "SOLO-T", "2026-01-01T09:00:00+00:00", 9)
    with _StatementCounter() as sc:
        body = _query(client, "SOLO-T").json()  # 默认门槛 2，车上仅 1 票
    assert body["segments"] == []
    # 车次 + 装卸后即早退，不装载货物行 / 每票上下文
    assert sc.count <= 2
