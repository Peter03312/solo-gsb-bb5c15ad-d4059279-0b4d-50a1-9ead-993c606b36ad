"""质控：单票货物**测温盲区**查询。

覆盖：
* 查询窗内自有测温 + 当前装卸窗口内、未被生效探头故障隔离的车载读数覆盖；
* 同刻自有测温优先（被取代车载读数不计覆盖）；越温读数不影响覆盖；
* 以查询边界与相邻有效读数时刻划分间隔，**严格长于**阈值才为盲区（等于不算）；
* 无有效读数时整窗作为一个间隔；查询窗左闭右开（读数在起点纳入、终点排除）；
* 盲区两端读数来源坐标（边界端为 null）；
* 按交接时刻切分保管方片段（交接点归接收方；恰在起点归接收方、恰在终点不切）、
  无交接时单片段、不产生零长度片段；
* 晚到读数、装卸窗口更正、故障登记 / 撤销后按当前状态重算（不改原记录）；
* 测温值更正只改温度、不改变覆盖；
* 参数 / 货物校验：缺参、非法 / 无时区起止、阈值非正、货物不存在。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.blindspots import custody_pieces, find_blind_spots
from app.chain import TimelinePoint, measurement_point


def _dt(second: int) -> datetime:
    """以“秒”构造 UTC 时刻，便于纯函数用整数式时间轴表达。"""
    return datetime(2026, 1, 1, tzinfo=timezone.utc).replace() + timedelta(seconds=second)


# ------------------------------------------------------------ 纯逻辑构造


def _own(at, eid=None):
    return measurement_point(_dt(at), 3.0, source="own", source_id=eid)


def _handover(at, giver="A", receiver="B"):
    return TimelinePoint(
        occurred_at=_dt(at),
        is_measurement=False,
        giver=giver,
        receiver=receiver,
        source="own",
    )


def _query(client, cargo, start="2026-01-01T00:00:00+00:00",
           end="2026-01-02T00:00:00+00:00", gap="3600"):
    return client.get(
        f"/cargo/{cargo}/blind-spots",
        params={"start_at": start, "end_at": end, "max_gap_seconds": gap},
    )


def _make_cargo(client, cargo="C1", holder="A仓库",
                started_at="2025-12-31T00:00:00+00:00", limit="5"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo, "started_at": started_at,
            "temp_upper_limit": limit, "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def _own_m(client, eid, cargo, at, temp="3"):
    r = client.post(
        "/events",
        json={"event_id": eid, "cargo_id": cargo,
              "occurred_at": at, "temperature": str(temp)},
    )
    assert r.status_code == 201, r.text


def _handover_m(client, eid, cargo, at, giver="A仓库", receiver="B仓库"):
    r = client.post(
        "/events",
        json={"event_id": eid, "cargo_id": cargo, "occurred_at": at,
              "giver": giver, "receiver": receiver},
    )
    assert r.status_code == 201, r.text


def _trip_manifest(client, trip="T1", cargo="C1",
                   loaded="2026-01-01T08:00:00+00:00",
                   unloaded="2026-01-01T18:00:00+00:00"):
    assert client.post(
        "/trips", json={"trip_id": trip, "vehicle_no": "v"}
    ).status_code == 201
    r = client.post(
        f"/trips/{trip}/manifest",
        json={"items": [{"cargo_id": cargo, "loaded_at": loaded,
                         "unloaded_at": unloaded}]},
    )
    assert r.status_code == 201, r.text


def _vr(client, rid, trip, at, temp="3"):
    r = client.post(
        "/vehicle-readings",
        json={"reading_id": rid, "trip_id": trip,
              "read_at": at, "temperature": str(temp)},
    )
    assert r.status_code in (200, 201), r.text


# ------------------------------------------------------------ 纯函数


def test_find_blind_spots_basic_partition():
    pts = [_own(1, "m1"), _own(4, "m2")]
    spots = find_blind_spots(pts, _dt(0), _dt(6), timedelta(seconds=2), "A")
    # 间隔 [0,1)=1s 不算；[1,4)=3s 盲区；[4,6)=2s 等于阈值不算
    assert [(s.start, s.end) for s in spots] == [(_dt(1), _dt(4))]
    spot = spots[0]
    assert spot.start_reading.event_id == "m1"
    assert spot.end_reading.event_id == "m2"
    assert spot.duration_seconds == 3.0
    # 无交接 -> 单片段，归初始保管方 A
    assert len(spot.custody_pieces) == 1
    assert [p.responsible_party for p in spot.custody_pieces] == ["A"]


def test_find_blind_spots_strictly_longer_than_threshold():
    pts = [_own(0, "m0"), _own(5, "m5")]
    # 间隔恰为 5s：阈值 5s -> 不算；阈值略小于 5s -> 算
    assert find_blind_spots(pts, _dt(0), _dt(5), timedelta(seconds=5), "A") == ()
    spots = find_blind_spots(
        pts, _dt(0), _dt(5), timedelta(seconds=4.999), "A"
    )
    assert [(s.start, s.end) for s in spots] == [(_dt(0), _dt(5))]


def test_find_blind_spots_no_readings_whole_window():
    spots = find_blind_spots([], _dt(2), _dt(10), timedelta(seconds=1), "A")
    assert len(spots) == 1
    spot = spots[0]
    assert (spot.start, spot.end) == (_dt(2), _dt(10))
    assert spot.start_reading is None and spot.end_reading is None
    # 整窗短于阈值 -> 无盲区
    assert find_blind_spots(
        [], _dt(2), _dt(10), timedelta(seconds=100), "A"
    ) == ()


def test_find_blind_spots_reading_at_end_excluded():
    # 读数恰在查询终点（左闭右开排除）-> 整窗盲区，右端为边界
    spots = find_blind_spots([_own(10, "m")], _dt(0), _dt(10),
                             timedelta(seconds=1), "A")
    assert len(spots) == 1
    assert (spots[0].start, spots[0].end) == (_dt(0), _dt(10))
    assert spots[0].start_reading is None and spots[0].end_reading is None
    # 读数恰在查询起点（纳入）-> 起点有读数，区间 [0,10) 右端边界
    spots = find_blind_spots([_own(0, "m0")], _dt(0), _dt(10),
                             timedelta(seconds=1), "A")
    assert spots[0].start_reading.event_id == "m0"
    assert spots[0].end_reading is None


def test_custody_pieces_split_at_handover():
    hs = [_handover(4, "A", "B"), _handover(7, "B", "C")]
    pieces = custody_pieces(_dt(0), _dt(10), hs, "A")
    assert [(p.start, p.end, p.responsible_party) for p in pieces] == [
        (_dt(0), _dt(4), "A"),
        (_dt(4), _dt(7), "B"),
        (_dt(7), _dt(10), "C"),
    ]
    assert [p.duration_seconds for p in pieces] == [4.0, 3.0, 3.0]


def test_custody_pieces_handover_at_start_goes_to_receiver():
    # 恰在起点的交接归接收方：起点保管方已是 B
    pieces = custody_pieces(_dt(5), _dt(10), [_handover(5, "A", "B")], "A")
    assert [(p.start, p.end, p.responsible_party) for p in pieces] == [
        (_dt(5), _dt(10), "B"),
    ]


def test_custody_pieces_handover_at_end_does_not_split():
    # 恰在终点（不含）的交接不切：末片段仍归交出方
    pieces = custody_pieces(_dt(0), _dt(5), [_handover(5, "A", "B")], "A")
    assert [(p.start, p.end, p.responsible_party) for p in pieces] == [
        (_dt(0), _dt(5), "A"),
    ]


def test_custody_pieces_prior_handover_sets_holder():
    # 起点之前的交接只把保管方推进到现任方
    pieces = custody_pieces(_dt(10), _dt(20), [_handover(3, "A", "X")], "A")
    assert [p.responsible_party for p in pieces] == ["X"]


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        find_blind_spots([], _dt(5), _dt(5), timedelta(seconds=1), "A")
    with pytest.raises(ValueError):
        find_blind_spots([], _dt(0), _dt(5), timedelta(seconds=0), "A")
    with pytest.raises(ValueError):
        find_blind_spots([], _dt(0), _dt(5), timedelta(seconds=-1), "A")


# ------------------------------------------------------------ API：覆盖口径


def test_own_measurements_partition_and_boundary_refs(client):
    _make_cargo(client)
    _own_m(client, "m1", "C1", "2026-01-01T00:00:00+00:00")
    _own_m(client, "m2", "C1", "2026-01-01T03:00:00+00:00")
    # 查询 [00,06)，阈值 1h：盲区 [m1,m2)=3h、[m2,06:00)=3h；[起点,m1) 零长
    data = _query(
        client, "C1",
        start="2026-01-01T00:00:00+00:00", end="2026-01-01T06:00:00+00:00",
    ).json()
    spots = data["blind_spots"]
    assert [(s["start"][11:16], s["end"][11:16]) for s in spots] == [
        ("00:00", "03:00"), ("03:00", "06:00"),
    ]
    first, second = spots
    assert first["start_reading"]["event_id"] == "m1"
    assert first["end_reading"]["event_id"] == "m2"
    assert second["start_reading"]["event_id"] == "m2"
    assert second["end_reading"] is None            # 查询终点边界
    assert first["duration_seconds"] == 3 * 3600
    assert first["custody_pieces"][0]["responsible_party"] == "A仓库"


def test_over_limit_reading_still_covers(client):
    _make_cargo(client, limit="5")
    # 两笔均越温（9℃ > 5），但间隔 30min 短于阈值 1h -> 无盲区
    _own_m(client, "m1", "C1", "2026-01-01T00:00:00+00:00", temp="9")
    _own_m(client, "m2", "C1", "2026-01-01T00:30:00+00:00", temp="9")
    data = _query(
        client, "C1",
        start="2026-01-01T00:00:00+00:00", end="2026-01-01T06:00:00+00:00",
    ).json()
    # [m2, 06:00) 为盲区，但 [m1,m2) 不是：越温不影响覆盖
    assert [(s["start"], s["end"]) for s in data["blind_spots"]] == [
        ("2026-01-01T00:30:00Z", "2026-01-01T06:00:00Z"),
    ]


def test_vehicle_reading_on_board_covers(client):
    _make_cargo(client)
    _trip_manifest(client)
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00")
    _vr(client, "r2", "T1", "2026-01-01T12:00:00+00:00")
    data = _query(
        client, "C1",
        start="2026-01-01T08:00:00+00:00", end="2026-01-01T18:00:00+00:00",
    ).json()
    # [08,r1)=1h 等于阈值不算；[r1,r2)=3h 盲区；[r2,18)=6h 盲区
    spots = data["blind_spots"]
    assert [(s["start"][11:16], s["end"][11:16]) for s in spots] == [
        ("09:00", "12:00"), ("12:00", "18:00"),
    ]
    ref = spots[0]["start_reading"]
    assert ref["source"] == "vehicle" and ref["reading_id"] == "r1"
    assert ref["trip_id"] == "T1" and ref["event_id"] is None


def test_vehicle_reading_off_loading_window_not_cover(client):
    _make_cargo(client)
    _trip_manifest(client)
    # r 在装车前 / 卸货后（左闭右开：恰在卸货时刻 18:00 也不在车）
    _vr(client, "r-before", "T1", "2026-01-01T07:30:00+00:00")
    _vr(client, "r-after", "T1", "2026-01-01T18:00:00+00:00")
    data = _query(
        client, "C1",
        start="2026-01-01T08:00:00+00:00", end="2026-01-01T18:00:00+00:00",
    ).json()
    # 窗内无任何有效读数 -> 整窗盲区，两端均为边界
    assert len(data["blind_spots"]) == 1
    spot = data["blind_spots"][0]
    assert spot["start_reading"] is None and spot["end_reading"] is None


def test_vehicle_reading_at_loaded_at_covers(client):
    _make_cargo(client)
    _trip_manifest(client)
    _vr(client, "r-edge", "T1", "2026-01-01T08:00:00+00:00")  # 恰在装车时刻纳入
    data = _query(
        client, "C1",
        start="2026-01-01T08:00:00+00:00", end="2026-01-01T10:00:00+00:00",
    ).json()
    spots = data["blind_spots"]
    # [r-edge, 10:00)=2h 盲区，起点读数是该车载点
    assert len(spots) == 1
    assert spots[0]["start_reading"]["reading_id"] == "r-edge"


def test_own_measurement_takes_precedence_same_instant(client):
    _make_cargo(client)
    _trip_manifest(client)
    _own_m(client, "o1", "C1", "2026-01-01T09:00:00+00:00", temp="2")
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00", temp="2")
    _vr(client, "r2", "T1", "2026-01-01T13:00:00+00:00", temp="2")
    data = _query(
        client, "C1",
        start="2026-01-01T08:00:00+00:00", end="2026-01-01T18:00:00+00:00",
    ).json()
    # [08,09)=1h 等于阈值不算；[o1(09),r2(13))=4h 盲区；[r2,18)=5h 盲区。
    # 09:00 同刻的覆盖读数来源是自有 o1（车载 r1 被取代）。
    spots = data["blind_spots"]
    assert [(s["start"][11:16], s["end"][11:16]) for s in spots] == [
        ("09:00", "13:00"), ("13:00", "18:00"),
    ]
    assert spots[0]["start_reading"]["source"] == "own"
    assert spots[0]["start_reading"]["event_id"] == "o1"
    assert spots[1]["start_reading"]["reading_id"] == "r2"


def test_quarantined_vehicle_reading_not_cover_then_restored(client):
    _make_cargo(client)
    _trip_manifest(client)
    _vr(client, "r1", "T1", "2026-01-01T09:00:00+00:00")
    _vr(client, "r2", "T1", "2026-01-01T10:30:00+00:00")
    # 故障覆盖 r2（10:00-11:00）-> r2 被隔离，09:00 之后直到 18:00 整段盲区
    r = client.post(
        "/trips/T1/probe-faults",
        json={
            "fault_id": "f1",
            "fault_start": "2026-01-01T10:00:00+00:00",
            "fault_end": "2026-01-01T11:00:00+00:00",
            "reason": "探头失准",
        },
    )
    assert r.status_code == 201, r.text
    q = dict(
        start_at="2026-01-01T08:00:00+00:00",
        end_at="2026-01-01T18:00:00+00:00",
        max_gap_seconds="3600",
    )
    spots = client.get("/cargo/C1/blind-spots", params=q).json()["blind_spots"]
    assert [(s["start"][11:16], s["end"][11:16]) for s in spots] == [
        ("09:00", "18:00"),
    ]
    assert spots[0]["start_reading"]["reading_id"] == "r1"
    # 撤销最后一条覆盖故障 -> r2 恢复覆盖：[r1,r2)=1.5h、[r2,18)=7.5h
    r = client.post(
        "/trips/T1/probe-faults/revoke",
        json={"revocation_id": "rv1", "fault_id": "f1", "expected_version": 0},
    )
    assert r.status_code == 201, r.text
    spots = client.get("/cargo/C1/blind-spots", params=q).json()["blind_spots"]
    assert [(s["start"][11:16], s["end"][11:16]) for s in spots] == [
        ("09:00", "10:30"), ("10:30", "18:00"),
    ]
    assert spots[0]["end_reading"]["reading_id"] == "r2"


def test_other_trip_reading_not_cover(client):
    _make_cargo(client)
    assert client.post("/trips", json={"trip_id": "T1", "vehicle_no": "v"}).status_code == 201
    assert client.post("/trips", json={"trip_id": "T2", "vehicle_no": "w"}).status_code == 201
    assert client.post(
        "/trips/T1/manifest",
        json={"items": [{"cargo_id": "C1",
                         "loaded_at": "2026-01-01T08:00:00+00:00",
                         "unloaded_at": "2026-01-01T18:00:00+00:00"}]},
    ).status_code == 201
    # 读数属于 T2，与 C1 的 T1 窗口无关
    _vr(client, "rx", "T2", "2026-01-01T10:00:00+00:00")
    data = _query(
        client, "C1",
        start="2026-01-01T08:00:00+00:00", end="2026-01-01T18:00:00+00:00",
    ).json()
    assert len(data["blind_spots"]) == 1
    assert data["blind_spots"][0]["start_reading"] is None


# ------------------------------------------------------------ API：保管方片段


def test_custody_pieces_in_api_response(client):
    _make_cargo(client, holder="A仓库")
    _own_m(client, "m1", "C1", "2026-01-01T00:00:00+00:00")
    _handover_m(client, "h1", "C1", "2026-01-01T02:00:00+00:00",
                giver="A仓库", receiver="B仓库")
    _handover_m(client, "h2", "C1", "2026-01-01T04:00:00+00:00",
                giver="B仓库", receiver="C仓库")
    data = _query(
        client, "C1",
        start="2026-01-01T00:00:00+00:00", end="2026-01-01T06:00:00+00:00",
    ).json()
    # 唯一盲区 [m1, 06:00)，按 02:00 / 04:00 切三段
    assert len(data["blind_spots"]) == 1
    pieces = data["blind_spots"][0]["custody_pieces"]
    assert [
        (p["start"][11:16], p["end"][11:16], p["responsible_party"])
        for p in pieces
    ] == [
        ("00:00", "02:00", "A仓库"),
        ("02:00", "04:00", "B仓库"),
        ("04:00", "06:00", "C仓库"),
    ]
    # 各片段时长之和等于盲区时长
    assert sum(p["duration_seconds"] for p in pieces) == (
        data["blind_spots"][0]["duration_seconds"]
    )


def test_handover_coincides_with_reading_instant(client):
    # 交接与读数同刻：该时刻盲区片段归接收方（同刻测温先于交接，片段自交接点归接收方）
    _make_cargo(client, holder="A仓库")
    _own_m(client, "m1", "C1", "2026-01-01T01:00:00+00:00")
    _handover_m(client, "h1", "C1", "2026-01-01T01:00:00+00:00",
                giver="A仓库", receiver="B仓库")
    data = _query(
        client, "C1",
        start="2026-01-01T00:00:00+00:00", end="2026-01-01T03:00:00+00:00",
    ).json()
    # 盲区 [01:00, 03:00)（[00,01)=1h 等于阈值不算）
    long_one = [b for b in data["blind_spots"] if b["start"][11:16] == "01:00"]
    assert len(long_one) == 1
    pieces = long_one[0]["custody_pieces"]
    assert [p["responsible_party"] for p in pieces] == ["B仓库"]


def test_no_handover_single_custody_piece(client):
    _make_cargo(client, holder="只此一家")
    data = _query(
        client, "C1",
        start="2026-01-01T00:00:00+00:00", end="2026-01-01T06:00:00+00:00",
    ).json()
    assert len(data["blind_spots"]) == 1
    pieces = data["blind_spots"][0]["custody_pieces"]
    assert len(pieces) == 1 and pieces[0]["responsible_party"] == "只此一家"


# ------------------------------------------------------------ API：重算 / 更正


def test_late_reading_recomputes(client):
    _make_cargo(client)
    q = dict(
        start_at="2026-01-01T00:00:00+00:00",
        end_at="2026-01-01T06:00:00+00:00",
        max_gap_seconds="3600",
    )
    assert client.get("/cargo/C1/blind-spots", params=q).json()["blind_spots"]
    _own_m(client, "late", "C1", "2026-01-01T02:00:00+00:00")
    spots = client.get("/cargo/C1/blind-spots", params=q).json()["blind_spots"]
    # 晚到读数把整窗盲区切成 [起点,02:00) 与 [02:00,终点)
    assert [(s["start"][11:16], s["end"][11:16]) for s in spots] == [
        ("00:00", "02:00"), ("02:00", "06:00"),
    ]


def test_temperature_correction_does_not_change_coverage(client):
    _make_cargo(client, limit="5")
    _own_m(client, "m1", "C1", "2026-01-01T00:00:00+00:00", temp="9")
    _own_m(client, "m2", "C1", "2026-01-01T00:30:00+00:00", temp="9")
    q = dict(
        start_at="2026-01-01T00:00:00+00:00",
        end_at="2026-01-01T06:00:00+00:00",
        max_gap_seconds="3600",
    )
    before = client.get("/cargo/C1/blind-spots", params=q).json()
    # 把 m1 更正为常温：只改温度，读数时刻不变，覆盖结果应完全一致
    r = client.post(
        "/corrections",
        json={
            "correction_id": "cor1", "target_type": "own_event",
            "target_id": "m1", "expected_version": 0, "new_temperature": "2",
        },
    )
    assert r.status_code == 201, r.text
    after = client.get("/cargo/C1/blind-spots", params=q).json()
    assert after == before


def test_loading_window_correction_recomputes_applicability(client):
    _make_cargo(client, started_at="2025-12-31T00:00:00+00:00")
    _trip_manifest(client)
    # r1 在原装车时刻 08:00 之前 -> 原不适用
    _vr(client, "r1", "T1", "2026-01-01T07:30:00+00:00")
    q = dict(
        start_at="2026-01-01T06:00:00+00:00",
        end_at="2026-01-01T12:00:00+00:00",
        max_gap_seconds="60",
    )
    before = client.get("/cargo/C1/blind-spots", params=q).json()["blind_spots"]
    assert len(before) == 1 and before[0]["start_reading"] is None
    # 把装车时刻更正到 07:00 -> r1 落入当前窗口，恢复覆盖
    r = client.post(
        "/trips/T1/loading-corrections",
        json={
            "correction_id": "lc1", "cargo_id": "C1",
            "origin_loaded_at": "2026-01-01T08:00:00+00:00",
            "expected_version": 0,
            "new_loaded_at": "2026-01-01T07:00:00+00:00",
            "new_unloaded_at": "2026-01-01T18:00:00+00:00",
            "reason": "装车时刻录晚",
        },
    )
    assert r.status_code == 201, r.text
    after = client.get("/cargo/C1/blind-spots", params=q).json()["blind_spots"]
    assert [(s["start"][11:16], s["end"][11:16]) for s in after] == [
        ("06:00", "07:30"), ("07:30", "12:00"),
    ]
    assert after[1]["start_reading"]["reading_id"] == "r1"


# ------------------------------------------------------------ API：错误 / 校验


def test_unknown_cargo_404(client):
    r = _query(client, "NOPE")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"


def test_invalid_query_window_422(client):
    _make_cargo(client)
    base = "http://x"
    for start, end in [
        ("2026-01-01T10:00:00+00:00", "2026-01-01T10:00:00+00:00"),
        ("2026-01-01T11:00:00+00:00", "2026-01-01T10:00:00+00:00"),
    ]:
        r = client.get(
            "/cargo/C1/blind-spots",
            params={"start_at": start, "end_at": end, "max_gap_seconds": "60"},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_query_window"


def test_naive_datetime_rejected(client):
    _make_cargo(client)
    r = client.get(
        "/cargo/C1/blind-spots",
        params={
            "start_at": "2026-01-01T00:00:00",
            "end_at": "2026-01-01T01:00:00+00:00",
            "max_gap_seconds": "60",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"


def test_malformed_datetime_rejected(client):
    _make_cargo(client)
    r = client.get(
        "/cargo/C1/blind-spots",
        params={
            "start_at": "not-a-time",
            "end_at": "2026-01-01T01:00:00+00:00",
            "max_gap_seconds": "60",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_query_window"


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5", "abc", "nan", "inf"])
def test_non_positive_or_bad_threshold_422(client, bad):
    _make_cargo(client)
    r = client.get(
        "/cargo/C1/blind-spots",
        params={
            "start_at": "2026-01-01T00:00:00+00:00",
            "end_at": "2026-01-01T01:00:00+00:00",
            "max_gap_seconds": bad,
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_gap_threshold"


def test_fractional_threshold_accepted(client):
    _make_cargo(client)
    _own_m(client, "m1", "C1", "2026-01-01T00:00:00.500+00:00")
    # 起点到 m1 间隔 0.5s；阈值 0.4s -> 盲区；阈值 0.5s -> 不算
    q = dict(
        start_at="2026-01-01T00:00:00+00:00",
        end_at="2026-01-01T00:00:01+00:00",
    )
    r1 = client.get("/cargo/C1/blind-spots", params={**q, "max_gap_seconds": "0.4"})
    spots = r1.json()["blind_spots"]
    assert any(s["end_reading"] and s["end_reading"]["event_id"] == "m1"
               for s in spots)
    r2 = client.get("/cargo/C1/blind-spots", params={**q, "max_gap_seconds": "0.5"})
    assert r2.json()["blind_spots"] == []


def test_missing_parameters_422(client):
    _make_cargo(client)
    base = {
        "start_at": "2026-01-01T00:00:00+00:00",
        "end_at": "2026-01-01T01:00:00+00:00",
        "max_gap_seconds": "60",
    }
    for dropped in ("start_at", "end_at", "max_gap_seconds"):
        params = {k: v for k, v in base.items() if k != dropped}
        r = client.get("/cargo/C1/blind-spots", params=params)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "missing_parameter"


def test_empty_window_when_all_gaps_short(client):
    _make_cargo(client)
    # 每分钟一笔读数、查询 10 分钟、阈值 30 分钟 -> 无盲区
    for i in range(11):
        _own_m(client, f"m{i}", "C1", f"2026-01-01T00:{i:02d}:00+00:00")
    data = _query(
        client, "C1",
        start="2026-01-01T00:00:00+00:00", end="2026-01-01T00:10:00+00:00",
        gap="1800",
    ).json()
    assert data["blind_spots"] == []
    assert data["max_gap_seconds"] == 1800.0
