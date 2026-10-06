"""亚秒级时刻精度：快照、表态有效性、新封存包与现况对照按实际时刻区分。

覆盖：
* 时间格式：整秒保持 ``YYYY-MM-DDTHH:MM:SSZ``（既有整秒标识与格式不变），
  亚秒追加 6 位微秒 ``.ffffffZ``；
* 快照标识：亚秒级边界变化（10:00:01.900 闭合后补录 10:00:01.100 提前闭合）
  产生不同快照；整秒区间快照与旧算法逐字节一致；
* 表态有效性：新表态按实际时刻记录片段坐标；补录提前闭合后旧表态失效；
  旧记录（整秒坐标、精度已丢失）对含亚秒的当前区间无法证实片段仍相同，
  不得认定有效（is_current=false）；整秒区间上的旧记录维持原判定；
* 新封存包：区间起止 / 片段 / 证据时刻按实际时刻（含亚秒）记录并计入摘要；
* 现况对照：亚秒级边界变化使对照为 changed 并逐项列出差异；旧封存包
  （整秒坐标）对含亚秒的当前状态不得对照为 unchanged；整秒旧包状态不变
  时仍为 unchanged；已封存原包与摘要不可改写。
"""
import hashlib
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import responsibility as resp
from app import sealing
from app.chain import rebuild_chain
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Event, EventType, EvidenceSeal, IntervalSource, Stance, StanceKind
from app.timeutil import format_utc
from decimal import Decimal

T = "2026-01-01T10:00"


# --------------------------------------------------------------- 工具


def _legacy_ts(dt: datetime) -> str:
    """旧算法：截为整秒的 UTC ISO 8601。"""
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def legacy_snapshot_id(start, end, segments) -> str:
    """旧算法快照标识（整秒截断），用于构造精度已丢失的旧记录。"""
    payload = [
        _legacy_ts(start),
        _legacy_ts(end) if end is not None else None,
        [[_legacy_ts(s), _legacy_ts(e) if e is not None else None, p] for s, e, p in segments],
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def setup_cargo(client, cargo="C1", limit="5", holder="A",
                start="2026-01-01T09:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def meas_at(client, eid, occurred_at, temp, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cargo,
            "occurred_at": occurred_at,
            "temperature": str(temp),
        },
    )


def first_interval(client, cargo="C1"):
    r = client.get(f"/cargo/{cargo}/responsibilities")
    assert r.status_code == 200
    intervals = r.json()["intervals"]
    assert intervals, "预期至少一个已闭合区间"
    return intervals[0]


def stance(client, iid, body, cargo="C1"):
    return client.post(
        f"/cargo/{cargo}/responsibilities/{iid}/stances", json=body
    )


def seal(client, case_id, cargo, interval_id, snapshot, expected=201):
    r = client.post(
        "/evidence-seals",
        json={
            "case_id": case_id,
            "cargo_id": cargo,
            "interval_id": interval_id,
            "snapshot_id": snapshot,
        },
    )
    assert r.status_code == expected, r.text
    return r


def build_subsecond_interval(client):
    """10:00:00.500 超限(A) 开启 -> 10:00:01.900 正常读数闭合。返回区间视图。"""
    setup_cargo(client)
    assert meas_at(client, "m1", f"{T}:00.500+00:00", 8).status_code == 201
    assert meas_at(client, "m2", f"{T}:01.900+00:00", 4).status_code == 201
    return first_interval(client)


def insert_legacy_stance(stance_id, cargo, interval_key, party, segments,
                         snapshot_id, kind=StanceKind.confirm):
    """直接落库一条旧格式（整秒截断坐标）表态行，模拟精度已丢失的历史记录。"""
    attested = json.dumps(
        [[_legacy_ts(s), _legacy_ts(e), p] for s, e, p in segments],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    db = SessionLocal()
    try:
        db.add(
            Stance(
                stance_id=stance_id,
                cargo_id=cargo,
                interval_key=interval_key,
                opener_source=IntervalSource.own,
                start_event_id=interval_key,
                vehicle_reading_id=None,
                responsible_party=party,
                kind=kind,
                reason=None,
                snapshot_id=snapshot_id,
                attested_segments=attested,
            )
        )
        db.commit()
    finally:
        db.close()
    return attested


def insert_legacy_seal(case_id, cargo="C1", interval_key="m1"):
    """直接落库一个旧格式（整秒截断时间）封存包，模拟精度已丢失的旧包。

    区间形状固定为 10:00:00.500 开启、10:00:01.900 闭合（旧包中截为整秒
    10:00:00 / 10:00:01），货物 C1（09:00 起始、上限 5、保管方 A）。
    """
    start = datetime(2026, 1, 1, 10, 0, 0, 500000, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 10, 0, 1, 900000, tzinfo=timezone.utc)
    segments = [(start, end, "A")]
    snap = legacy_snapshot_id(start, end, segments)
    content = {
        "case_id": case_id,
        "sealed_at": "2026-01-01T10:05:00Z",
        "cargo": {
            "cargo_id": cargo,
            "started_at": "2026-01-01T09:00:00Z",
            "temp_upper_limit": "5.000",
            "initial_holder": "A",
        },
        "interval": {
            "interval_id": interval_key,
            "start": "2026-01-01T10:00:00Z",
            "end": "2026-01-01T10:00:01Z",
            "is_open": False,
            "start_source": "own",
            "end_source": "own",
            "start_event_id": "m1",
            "end_event_id": "m2",
            "start_reading_id": None,
            "end_reading_id": None,
            "start_trip_id": None,
            "end_trip_id": None,
            "snapshot_id": snap,
            "segments": [["2026-01-01T10:00:00Z", "2026-01-01T10:00:01Z", "A"]],
        },
        "current_valid_stances": [],
        "summary": "pending",
        "own_events": [
            {
                "event_id": "m1", "event_type": "measurement",
                "occurred_at": "2026-01-01T10:00:00Z",
                "temperature": "8.000", "effective_temperature": "8.000",
                "version": 0, "temp_upper_limit": "5.000",
                "giver": None, "receiver": None,
            },
            {
                "event_id": "m2", "event_type": "measurement",
                "occurred_at": "2026-01-01T10:00:01Z",
                "temperature": "4.000", "effective_temperature": "4.000",
                "version": 0, "temp_upper_limit": "5.000",
                "giver": None, "receiver": None,
            },
        ],
        "vehicle_readings": [],
        "loading_windows": [],
        "temp_standards": [
            {
                "version": 0,
                "effective_at": "2026-01-01T09:00:00Z",
                "temp_upper_limit": "5.000",
                "change_id": None,
                "reason": None,
            }
        ],
    }
    canonical = sealing.canonical_json(content)
    db = SessionLocal()
    try:
        db.add(
            EvidenceSeal(
                case_id=case_id,
                cargo_id=cargo,
                interval_key=interval_key,
                snapshot_id=snap,
                request_hash="sha256:" + "0" * 64,
                content=canonical,
                content_digest=sealing.digest_of(canonical),
                sealed_at=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()
    return snap, content


# --------------------------------------------------------------- 单元：时间格式


def test_format_utc_whole_second_keeps_legacy_shape():
    dt = datetime(2026, 1, 1, 10, 0, 1, tzinfo=timezone.utc)
    assert format_utc(dt) == "2026-01-01T10:00:01Z"
    # 无时区按 UTC 解释
    assert format_utc(datetime(2026, 1, 1, 10, 0, 1)) == "2026-01-01T10:00:01Z"


def test_format_utc_subsecond_appends_microseconds():
    assert (
        format_utc(datetime(2026, 1, 1, 10, 0, 1, 900000, tzinfo=timezone.utc))
        == "2026-01-01T10:00:01.900000Z"
    )
    assert (
        format_utc(datetime(2026, 1, 1, 10, 0, 1, 100000, tzinfo=timezone.utc))
        == "2026-01-01T10:00:01.100000Z"
    )
    # 非 UTC 时区换算后保留亚秒
    from datetime import timedelta

    dt = datetime(2026, 1, 1, 18, 0, 1, 500000, tzinfo=timezone(timedelta(hours=8)))
    assert format_utc(dt) == "2026-01-01T10:00:01.500000Z"


def _meas_event(eid, at, temp):
    return Event(
        event_id=eid, cargo_id="C1", event_type=EventType.measurement,
        occurred_at=at, temperature=Decimal(temp),
    )


def _subsecond_excursion(close_us=900000):
    t0 = datetime(2026, 1, 1, 10, 0, 0, 500000, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 10, 0, 1, close_us, tzinfo=timezone.utc)
    r = rebuild_chain(
        "A", Decimal("5"), [_meas_event("m1", t0, "8"), _meas_event("m2", t1, "4")]
    )
    return r.excursions[0]


def test_snapshot_distinguishes_subsecond_boundaries():
    ex_900 = _subsecond_excursion(900000)
    ex_100 = _subsecond_excursion(100000)
    assert ex_900.end != ex_100.end
    assert resp.snapshot_id(ex_900) != resp.snapshot_id(ex_100)
    # 同一实际时刻 -> 快照稳定
    assert resp.snapshot_id(_subsecond_excursion(900000)) == resp.snapshot_id(ex_900)


def test_snapshot_whole_second_identical_to_legacy_algorithm():
    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
    r = rebuild_chain(
        "A", Decimal("5"), [_meas_event("m1", t0, "8"), _meas_event("m2", t1, "4")]
    )
    ex = r.excursions[0]
    legacy = legacy_snapshot_id(t0, t1, [(t0, t1, "A")])
    # 整秒时刻：新算法与旧算法逐字节一致（既有快照标识不变）
    assert resp.snapshot_id(ex) == legacy


def test_attested_segments_json_carries_subsecond():
    ex = _subsecond_excursion(900000)
    attested = resp.attested_segments_json(ex, "A")
    assert "2026-01-01T10:00:01.900000Z" in attested
    assert "2026-01-01T10:00:00.500000Z" in attested


def test_stance_current_subsecond_boundary_shift_invalidates():
    ex_900 = _subsecond_excursion(900000)
    stance = Stance(
        stance_id="s1", cargo_id="C1", interval_key="m1",
        opener_source=IntervalSource.own, start_event_id="m1",
        vehicle_reading_id=None, responsible_party="A",
        kind=StanceKind.confirm, reason=None,
        snapshot_id=resp.snapshot_id(ex_900),
        attested_segments=resp.attested_segments_json(ex_900, "A"),
    )
    assert resp.is_stance_current(stance, _subsecond_excursion(900000)) is True
    # 补录更早闭合读数：边界亚秒级提前 -> 旧表态失效
    assert resp.is_stance_current(stance, _subsecond_excursion(100000)) is False


def test_sealing_ts_matches_format_utc():
    dt = datetime(2026, 1, 1, 10, 0, 1, 900000, tzinfo=timezone.utc)
    assert sealing.ts(dt) == "2026-01-01T10:00:01.900000Z"
    assert sealing.ts(datetime(2026, 1, 1, 10, 0, 1, tzinfo=timezone.utc)) == (
        "2026-01-01T10:00:01Z"
    )


# --------------------------------------------------------------- 集成：亚秒场景


def test_subsecond_close_then_earlier_backfill_full_flow(client):
    """10:00:01.900 正常读数闭合区间；补录 10:00:01.100 正常读数使其提前闭合：
    快照变化、旧表态失效、封存对照 changed，原包不改写。"""
    iv = build_subsecond_interval(client)
    assert iv["end"] == "2026-01-01T10:00:01.900000Z"
    assert iv["start"] == "2026-01-01T10:00:00.500000Z"
    snap1 = iv["snapshot_id"]

    # 报告与时间线同样按实际时刻给出亚秒
    report = client.get("/cargo/C1/report").json()
    assert report["excursions"][0]["end"] == "2026-01-01T10:00:01.900000Z"

    # 表态确认（新表态按实际时刻记录片段坐标）
    r = stance(
        client, "m1",
        {"snapshot_id": snap1, "responsible_party": "A",
         "stance": "confirm", "stance_id": "s1"},
    )
    assert r.status_code == 201, r.text
    db = SessionLocal()
    try:
        row = db.get(Stance, "s1")
        assert ".900000Z" in row.attested_segments
    finally:
        db.close()

    # 封存：新封存包按实际时刻记录亚秒边界
    created = seal(client, "CASE-SUB-1", "C1", "m1", snap1).json()
    assert created["content"]["interval"]["end"] == "2026-01-01T10:00:01.900000Z"
    m2_item = next(
        e for e in created["content"]["own_events"] if e["event_id"] == "m2"
    )
    assert m2_item["occurred_at"] == "2026-01-01T10:00:01.900000Z"

    # 无变化时对照为 unchanged
    d0 = client.get("/evidence-seals/CASE-SUB-1/diff").json()
    assert d0["status"] == "unchanged"
    assert d0["segment_changes"] == []

    # 补录 10:00:01.100 正常读数 -> 区间提前闭合
    assert meas_at(client, "m0", f"{T}:01.100+00:00", 3).status_code == 201

    iv2 = first_interval(client)
    assert iv2["end"] == "2026-01-01T10:00:01.100000Z"
    assert iv2["snapshot_id"] != snap1

    # 旧表态失效（历史保留）
    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["history"][0]["stance_id"] == "s1"
    assert hist["history"][0]["is_current"] is False
    assert hist["current_snapshot_id"] == iv2["snapshot_id"]

    # 旧快照表态被拒（过期）；新快照可表态
    r = stance(
        client, "m1",
        {"snapshot_id": snap1, "responsible_party": "A",
         "stance": "confirm", "stance_id": "s2"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_snapshot"
    r = stance(
        client, "m1",
        {"snapshot_id": iv2["snapshot_id"], "responsible_party": "A",
         "stance": "confirm", "stance_id": "s2"},
    )
    assert r.status_code == 201, r.text

    # 封存对照：changed，逐项列出亚秒差异
    d = client.get("/evidence-seals/CASE-SUB-1/diff").json()
    assert d["status"] == "changed"
    assert d["sealed_snapshot_id"] == snap1
    assert d["current_snapshot_id"] == iv2["snapshot_id"]
    assert d["current_interval"]["end"] == "2026-01-01T10:00:01.100000Z"
    seg = next(x for x in d["segment_changes"] if x["change"] == "changed")
    assert seg["sealed"][1] == "2026-01-01T10:00:01.900000Z"
    assert seg["current"][1] == "2026-01-01T10:00:01.100000Z"
    added = next(
        x for x in d["evidence_changes"]["own_events"] if x["key"] == "m0"
    )
    assert added["change"] == "added"
    assert added["current"]["occurred_at"] == "2026-01-01T10:00:01.100000Z"

    # 已封存原包与摘要不可改写
    got = client.get("/evidence-seals/CASE-SUB-1").json()
    assert got["content"] == created["content"]
    assert got["content_digest"] == created["content_digest"]
    assert got["content"]["interval"]["end"] == "2026-01-01T10:00:01.900000Z"

    # 同案件编号同请求重试：即使现况已变仍返回原包
    r = seal(client, "CASE-SUB-1", "C1", "m1", snap1, expected=200)
    assert r.json()["replayed"] is True
    assert r.json()["seal"]["content"] == created["content"]

    # 按新快照可另案封存
    assert seal(client, "CASE-SUB-2", "C1", "m1", iv2["snapshot_id"]).status_code == 201


def test_whole_second_identifiers_and_format_unchanged(client):
    """整秒时刻：区间坐标、快照与封存内容保持既有整秒格式（无小数秒）。"""
    setup_cargo(client)
    assert meas_at(client, "m1", f"{T}:00+00:00", 8).status_code == 201
    assert meas_at(client, "m2", f"{T}:05+00:00", 4).status_code == 201
    iv = first_interval(client)
    assert iv["start"] == "2026-01-01T10:00:00Z"
    assert iv["end"] == "2026-01-01T10:00:05Z"
    assert iv["snapshot_id"].startswith("sha256:")
    assert len(iv["snapshot_id"]) == 7 + 64

    created = seal(client, "CASE-WS", "C1", "m1", iv["snapshot_id"]).json()
    interval = created["content"]["interval"]
    assert interval["start"] == "2026-01-01T10:00:00Z"
    assert interval["end"] == "2026-01-01T10:00:05Z"
    assert interval["segments"] == [
        {
            "start": "2026-01-01T10:00:00Z",
            "end": "2026-01-01T10:00:05Z",
            "responsible_party": "A",
        }
    ]
    m1_item = next(
        e for e in created["content"]["own_events"] if e["event_id"] == "m1"
    )
    assert m1_item["occurred_at"] == "2026-01-01T10:00:00Z"

    d = client.get("/evidence-seals/CASE-WS/diff").json()
    assert d["status"] == "unchanged"
    assert d["segment_changes"] == []


def test_legacy_truncated_stance_not_current_on_subsecond_interval(client):
    """旧表态（整秒截断坐标、精度已丢失）对含亚秒的当前区间：
    无法证实片段仍相同 -> 不得认定有效。"""
    iv = build_subsecond_interval(client)
    start = datetime(2026, 1, 1, 10, 0, 0, 500000, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 10, 0, 1, 900000, tzinfo=timezone.utc)
    legacy_snap = legacy_snapshot_id(start, end, [(start, end, "A")])
    attested = insert_legacy_stance(
        "legacy-s1", "C1", "m1", "A", [(start, end, "A")], legacy_snap
    )
    assert ".900000" not in attested  # 旧记录确为整秒坐标

    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1
    assert hist["history"][0]["is_current"] is False
    # 责任确认视图中不出现有效表态
    iv2 = first_interval(client)
    assert iv2["latest_stances"] == []
    assert iv2["summary"] == "pending"
    assert iv2["snapshot_id"] != legacy_snap


def test_legacy_truncated_stance_still_current_on_whole_second_interval(client):
    """整秒区间上的旧表态（整秒坐标与实际一致）：维持原有有效判定。"""
    setup_cargo(client)
    assert meas_at(client, "m1", f"{T}:00+00:00", 8).status_code == 201
    assert meas_at(client, "m2", f"{T}:05+00:00", 4).status_code == 201
    start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
    legacy_snap = legacy_snapshot_id(start, end, [(start, end, "A")])
    insert_legacy_stance(
        "legacy-s2", "C1", "m1", "A", [(start, end, "A")], legacy_snap
    )

    hist = client.get("/cargo/C1/responsibilities/m1/stances").json()
    assert hist["history"][0]["is_current"] is True
    iv = first_interval(client)
    # 整秒时刻快照标识与旧算法一致
    assert iv["snapshot_id"] == legacy_snap
    assert [s["stance_id"] for s in iv["latest_stances"]] == ["legacy-s2"]
    assert iv["summary"] == "all_confirmed"


def test_legacy_truncated_seal_diff_changed_on_subsecond_interval(client):
    """旧封存包（整秒截断时间、精度已丢失）对含亚秒的当前区间：
    不得对照为 unchanged，差异经现有字段逐项呈现；原包不改写。"""
    build_subsecond_interval(client)
    legacy_snap, content = insert_legacy_seal("LEG-SUB")

    d = client.get("/evidence-seals/LEG-SUB/diff").json()
    assert d["status"] == "changed"
    assert d["sealed_snapshot_id"] == legacy_snap
    assert d["current_snapshot_id"] != legacy_snap
    # 片段起点亚秒级不同：旧片段 removed、新片段 added
    changes = {(x["start"], x["change"]) for x in d["segment_changes"]}
    assert ("2026-01-01T10:00:00Z", "removed") in changes
    assert ("2026-01-01T10:00:00.500000Z", "added") in changes
    # 证据时刻差异逐项呈现（整秒旧值 vs 亚秒新值）
    own = {x["key"]: x for x in d["evidence_changes"]["own_events"]}
    assert own["m1"]["change"] == "changed"
    assert "occurred_at" in own["m1"]["fields"]
    assert own["m1"]["sealed"]["occurred_at"] == "2026-01-01T10:00:00Z"
    assert own["m1"]["current"]["occurred_at"] == "2026-01-01T10:00:00.500000Z"
    assert own["m2"]["current"]["occurred_at"] == "2026-01-01T10:00:01.900000Z"

    # 原包与摘要不可改写：GET 恒返回旧格式原文
    got = client.get("/evidence-seals/LEG-SUB").json()
    assert got["content"]["interval"]["end"] == "2026-01-01T10:00:01Z"
    assert got["content"]["interval"]["snapshot_id"] == legacy_snap
    assert got["content_digest"] == sealing.digest_of(sealing.canonical_json(content))


def test_legacy_whole_second_seal_unchanged_when_state_same(client):
    """整秒旧封存包、当前状态不变：快照一致，对照仍为 unchanged。"""
    setup_cargo(client)
    assert meas_at(client, "m1", f"{T}:00+00:00", 8).status_code == 201
    assert meas_at(client, "m2", f"{T}:05+00:00", 4).status_code == 201

    # 旧包内容（整秒坐标与实际一致，旧算法快照 == 新算法快照）
    start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc)
    snap = legacy_snapshot_id(start, end, [(start, end, "A")])
    content = {
        "case_id": "LEG-WS",
        "sealed_at": "2026-01-01T10:05:00Z",
        "cargo": {
            "cargo_id": "C1",
            "started_at": "2026-01-01T09:00:00Z",
            "temp_upper_limit": "5.000",
            "initial_holder": "A",
        },
        "interval": {
            "interval_id": "m1",
            "start": "2026-01-01T10:00:00Z",
            "end": "2026-01-01T10:00:05Z",
            "is_open": False,
            "start_source": "own",
            "end_source": "own",
            "start_event_id": "m1",
            "end_event_id": "m2",
            "start_reading_id": None,
            "end_reading_id": None,
            "start_trip_id": None,
            "end_trip_id": None,
            "snapshot_id": snap,
            "segments": [["2026-01-01T10:00:00Z", "2026-01-01T10:00:05Z", "A"]],
        },
        "current_valid_stances": [],
        "summary": "pending",
        "own_events": [
            {
                "event_id": "m1", "event_type": "measurement",
                "occurred_at": "2026-01-01T10:00:00Z",
                "temperature": "8.000", "effective_temperature": "8.000",
                "version": 0, "temp_upper_limit": "5.000",
                "giver": None, "receiver": None,
            },
            {
                "event_id": "m2", "event_type": "measurement",
                "occurred_at": "2026-01-01T10:00:05Z",
                "temperature": "4.000", "effective_temperature": "4.000",
                "version": 0, "temp_upper_limit": "5.000",
                "giver": None, "receiver": None,
            },
        ],
        "vehicle_readings": [],
        "loading_windows": [],
        "temp_standards": [
            {
                "version": 0,
                "effective_at": "2026-01-01T09:00:00Z",
                "temp_upper_limit": "5.000",
                "change_id": None,
                "reason": None,
            }
        ],
    }
    canonical = sealing.canonical_json(content)
    db = SessionLocal()
    try:
        db.add(
            EvidenceSeal(
                case_id="LEG-WS",
                cargo_id="C1",
                interval_key="m1",
                snapshot_id=snap,
                request_hash="sha256:" + "0" * 64,
                content=canonical,
                content_digest=sealing.digest_of(canonical),
                sealed_at=datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc),
            )
        )
        db.commit()
    finally:
        db.close()

    d = client.get("/evidence-seals/LEG-WS/diff").json()
    assert d["status"] == "unchanged"
    assert d["current_snapshot_id"] == snap
    assert d["segment_changes"] == []
    assert d["evidence_changes"]["own_events"] == []


def test_subsecond_handover_splits_segments_precisely(client):
    """亚秒级交接时刻：责任片段在亚秒边界拆分，快照 / 片段坐标 / 封存内容均精确。"""
    setup_cargo(client)
    assert meas_at(client, "m1", f"{T}:00.500+00:00", 8).status_code == 201
    r = client.post(
        "/events",
        json={
            "event_id": "h1", "cargo_id": "C1",
            "occurred_at": f"{T}:01.200+00:00",
            "giver": "A", "receiver": "B",
        },
    )
    assert r.status_code == 201, r.text
    assert meas_at(client, "m2", f"{T}:01.900+00:00", 4).status_code == 201

    iv = first_interval(client)
    assert [
        (s["start"], s["end"], s["responsible_party"]) for s in iv["segments"]
    ] == [
        ("2026-01-01T10:00:00.500000Z", "2026-01-01T10:00:01.200000Z", "A"),
        ("2026-01-01T10:00:01.200000Z", "2026-01-01T10:00:01.900000Z", "B"),
    ]
    snap = iv["snapshot_id"]
    # A 只对第一段表态；其片段坐标精确到亚秒
    assert stance(
        client, "m1",
        {"snapshot_id": snap, "responsible_party": "A",
         "stance": "confirm", "stance_id": "sa"},
    ).status_code == 201
    db = SessionLocal()
    try:
        row = db.get(Stance, "sa")
        assert "2026-01-01T10:00:01.200000Z" in row.attested_segments
        assert "2026-01-01T10:00:01.900000Z" not in row.attested_segments
    finally:
        db.close()

    created = seal(client, "CASE-HND", "C1", "m1", snap).json()
    segs = created["content"]["interval"]["segments"]
    assert segs[0]["end"] == "2026-01-01T10:00:01.200000Z"
    assert segs[1]["start"] == "2026-01-01T10:00:01.200000Z"
    d = client.get("/evidence-seals/CASE-HND/diff").json()
    assert d["status"] == "unchanged"


def test_vehicle_reading_subsecond_interval_and_backfill(client):
    """车载读数亚秒闭合：补录更早车载读数提前闭合 -> 快照 / 表态 / 对照随之区分。"""
    assert client.post(
        "/cargo",
        json={
            "cargo_id": "CV",
            "started_at": "2026-01-01T09:00:00+00:00",
            "temp_upper_limit": "4.0",
            "initial_holder": "A",
        },
    ).status_code == 201
    assert client.post(
        "/trips", json={"trip_id": "TV", "vehicle_no": "v1"}
    ).status_code == 201
    assert client.post(
        "/trips/TV/manifest",
        json={
            "items": [
                {
                    "cargo_id": "CV",
                    "loaded_at": "2026-01-01T09:30:00+00:00",
                    "unloaded_at": "2026-01-01T18:00:00+00:00",
                }
            ]
        },
    ).status_code == 201
    for rid, at, t in [
        ("vr1", "2026-01-01T10:00:00.250+00:00", "7"),
        ("vr2", "2026-01-01T10:00:01.750+00:00", "3"),
    ]:
        assert client.post(
            "/vehicle-readings",
            json={
                "reading_id": rid, "trip_id": "TV",
                "read_at": at, "temperature": t,
            },
        ).status_code == 201

    iv = first_interval(client, "CV")
    assert iv["interval_id"] == "veh:vr1"
    assert iv["end"] == "2026-01-01T10:00:01.750000Z"
    snap1 = iv["snapshot_id"]
    assert stance(
        client, "veh:vr1",
        {"snapshot_id": snap1, "responsible_party": "A",
         "stance": "confirm", "stance_id": "sv1"},
        cargo="CV",
    ).status_code == 201
    created = seal(client, "CASE-VSUB", "CV", "veh:vr1", snap1).json()
    vr2_item = next(
        r for r in created["content"]["vehicle_readings"] if r["reading_id"] == "vr2"
    )
    assert vr2_item["read_at"] == "2026-01-01T10:00:01.750000Z"

    # 补录 10:00:01.250 不超限车载读数 -> 区间提前闭合
    assert client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr0", "trip_id": "TV",
            "read_at": "2026-01-01T10:00:01.250+00:00", "temperature": "2",
        },
    ).status_code == 201

    iv2 = first_interval(client, "CV")
    assert iv2["end"] == "2026-01-01T10:00:01.250000Z"
    assert iv2["snapshot_id"] != snap1
    hist = client.get("/cargo/CV/responsibilities/veh:vr1/stances").json()
    assert hist["history"][0]["is_current"] is False

    d = client.get("/evidence-seals/CASE-VSUB/diff").json()
    assert d["status"] == "changed"
    vr0 = next(
        x for x in d["evidence_changes"]["vehicle_readings"] if x["key"] == "vr0"
    )
    assert vr0["change"] == "added"
    assert vr0["current"]["read_at"] == "2026-01-01T10:00:01.250000Z"
    # 原包不改写
    got = client.get("/evidence-seals/CASE-VSUB").json()
    assert got["content"] == created["content"]
