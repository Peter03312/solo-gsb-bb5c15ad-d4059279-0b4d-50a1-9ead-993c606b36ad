"""车次装卸批量登记：数据库操作异常（OperationalError）后的恢复裁决。

OperationalError 既可能在**提交前**抛出（锁等待耗尽 / 死锁中止，事务已整批
回滚），也可能在 commit **已落库后**才由驱动抛出——调用方无法从异常本身区分。
修复要求的恢复裁决：

* 仅当能确认**本次完整批次已整体落库**、且每条窗口的**原始装卸时刻**都与请求
  逐条一致（车次 + 货物 + 首次装车时刻 + 首次登记卸车时刻）时，才按成功回放；
* 仅装车时刻相同的旧窗口 / 其他同刻窗口（旧 08:00-10:00，请求 08:00-12:00）
  不得伪装成功：沿原事务重试，旧窗口重叠仍按 ``loading_overlap`` 拒绝；
* 携预检摘要的确认登记同样裁决；本批未落库时摘要过期仍
  ``stale_precheck_digest`` 拒绝；
* 部分落库 / 缺条不得伪装成功；
* 提交后异常的回放不得重复登记、不得再次推进车次修订号。

异常注入方式（与 tests/test_evidence_seal.py 同思路）：
* 让请求事务的首个 ``Session.get`` 抛一次 OperationalError —— 模拟提交前中止；
* 让 ``Session.commit`` 在真实提交成功后抛一次 OperationalError —— 模拟
  “commit 已落库但驱动报错”的歧义时刻。
"""
import sqlalchemy.orm as orm
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Loading
from app.timeutil import ensure_utc
from datetime import datetime, timezone


def _dt(hour):
    return datetime(2026, 1, 1, hour, tzinfo=timezone.utc)


def _setup_trip_cargo(client, trip="TR", cargos=("C1",)):
    for cid in cargos:
        r = client.post(
            "/cargo",
            json={
                "cargo_id": cid,
                "started_at": "2026-01-01T00:00:00+00:00",
                "temp_upper_limit": "4.0",
                "initial_holder": "北京冷库",
            },
        )
        assert r.status_code == 201, r.text
    r = client.post("/trips", json={"trip_id": trip, "vehicle_no": "京A-1"})
    assert r.status_code == 201, r.text


def _manifest(client, trip, items):
    return client.post(f"/trips/{trip}/manifest", json={"items": items})


def _item(cid, lo, un):
    return {
        "cargo_id": cid,
        "loaded_at": f"2026-01-01T{lo}:00+00:00",
        "unloaded_at": f"2026-01-01T{un}:00+00:00",
    }


def _fail_first_get_with_operational_error(monkeypatch):
    """下一个请求事务的首个 Session.get 抛 OperationalError（仅一次）。

    模拟提交前中止：_register_manifest_tx 在任何写入前即失败，整批未落库。
    恢复探测使用 scalars（不消耗 get），重试事务的首个 get 恢复放行。
    """
    state = {"armed": True}
    real_get = orm.Session.get

    def flaky_get(self, entity, *args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise OperationalError(
                "SELECT ... (simulated lock/deadlock abort before commit)",
                {},
                Exception("simulated operational failure before commit"),
            )
        return real_get(self, entity, *args, **kwargs)

    monkeypatch.setattr(orm.Session, "get", flaky_get)
    return state


def _commit_raises_after_real_commit(monkeypatch):
    """Session.commit 在真实提交成功后抛一次 OperationalError。

    模拟“commit 已落库但驱动 / 连接层报错”：恢复路径必须探测到整批已落库并
    回放，且绝不能再次执行写入事务。
    """
    state = {"armed": True, "commits": 0}
    real_commit = orm.Session.commit

    def flaky_commit(self):
        real_commit(self)
        state["commits"] += 1
        if state["armed"]:
            state["armed"] = False
            raise OperationalError(
                "COMMIT ... (simulated post-commit driver failure)",
                {},
                Exception("simulated operational failure after commit"),
            )

    monkeypatch.setattr(orm.Session, "commit", flaky_commit)
    return state


def _trip_detail(client, trip):
    return client.get(f"/trips/{trip}").json()


def _loading_map(client, trip):
    d = _trip_detail(client, trip)
    return {
        (l["cargo_id"], l["loaded_at"][11:16], l["unloaded_at"][11:16]): l
        for l in d["loadings"]
    }


# ----------------------------------------------------------- 直接登记


def test_direct_register_precommit_failure_overlapping_old_window_rejected(
    client, monkeypatch
):
    """旧窗口 08:00-10:00 已存在；同票再请 08:00-12:00，提交前失败。

    旧恢复逻辑仅凭车次 + 货物 + 首次装车时刻（08:00）即误判整批已提交，回传
    旧窗口 10:00 卸车当作成功。修复后必须重试原事务并按重叠拒绝 409。
    """
    _setup_trip_cargo(client, cargos=("C1",))
    assert _manifest(client, "TR", [_item("C1", "08:00", "10:00")]).status_code == 201
    rev_before = _trip_detail(client, "TR")["revision"]

    _fail_first_get_with_operational_error(monkeypatch)
    r = _manifest(client, "TR", [_item("C1", "08:00", "12:00")])
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "loading_overlap"

    d = _trip_detail(client, "TR")
    # 旧窗口原样保留（卸车 10:00），没有 12:00 的新窗口；修订号未推进
    assert [
        (l["cargo_id"], l["unloaded_at"][11:16]) for l in d["loadings"]
    ] == [("C1", "10:00")]
    assert d["revision"] == rev_before


def test_direct_register_precommit_failure_clean_db_retries_and_succeeds(
    client, monkeypatch
):
    """提交前失败但无任何冲突：恢复探测查无本批 -> 重试原事务 -> 201 落库。"""
    _setup_trip_cargo(client, cargos=("C1", "C2"))

    _fail_first_get_with_operational_error(monkeypatch)
    r = _manifest(
        client,
        "TR",
        [_item("C1", "08:00", "12:00"), _item("C2", "08:00", "13:00")],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    # 返回的是本次请求的窗口（12:00 / 13:00），不是任何旧窗口
    assert sorted(
        (it["cargo_id"], it["unloaded_at"][11:16]) for it in body["items"]
    ) == [("C1", "12:00"), ("C2", "13:00")]

    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 2
    assert d["revision"] == 1  # 仅重试成功的那一次 +1，失败尝试不计


def test_direct_register_postcommit_failure_replays_exact_batch_without_duplicate(
    client, monkeypatch
):
    """commit 已落库后抛错：探测到整批与原始装卸时刻逐条一致 -> 回放成功。

    不重复登记、修订号只 +1，回传每条**本次请求**的卸车时刻。
    """
    _setup_trip_cargo(client, cargos=("C1", "C2"))
    state = _commit_raises_after_real_commit(monkeypatch)

    r = _manifest(
        client,
        "TR",
        [_item("C1", "08:00", "12:00"), _item("C2", "08:00", "13:00")],
    )
    assert r.status_code == 201, r.text
    assert sorted(
        (it["cargo_id"], it["unloaded_at"][11:16]) for it in r.json()["items"]
    ) == [("C1", "12:00"), ("C2", "13:00")]
    assert state["commits"] == 1  # 只有首次事务真实提交；回放不得再写

    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 2  # 无重复登记
    assert d["revision"] == 1


def test_direct_register_partial_batch_and_other_same_time_window_not_faked(
    client, monkeypatch
):
    """两种“非整批”状态都不得伪装成功：

    a) C1 已有旧窗口 08:00-10:00（其他同刻窗口），本批为 C1 08:00-12:00 +
       C2 08:00-13:00：C1 装车时刻虽同为 08:00，但原始卸车时刻 10:00≠12:00；
    b) 直接向库里只插入本批 C1 08:00-12:00 一行（部分落库），C2 缺失。
    两种情况下提交前再失败，都必须重试并按冲突拒绝，不留新记录、不 +1。
    """
    _setup_trip_cargo(client, cargos=("C1", "C2"))
    assert _manifest(client, "TR", [_item("C1", "08:00", "10:00")]).status_code == 201
    rev_before = _trip_detail(client, "TR")["revision"]

    _fail_first_get_with_operational_error(monkeypatch)
    r = _manifest(
        client,
        "TR",
        [_item("C1", "08:00", "12:00"), _item("C2", "08:00", "13:00")],
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "loading_overlap"
    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 1 and d["loadings"][0]["unloaded_at"][11:16] == "10:00"
    assert d["revision"] == rev_before

    # --- b) 部分落库：库里只有本批 C1 一条，缺 C2 ---
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _setup_trip_cargo(client, cargos=("C1", "C2"))
    db = SessionLocal()
    try:
        db.add(
            Loading(
                trip_id="TR",
                cargo_id="C1",
                origin_loaded_at=_dt(8),
                loaded_at=_dt(8),
                unloaded_at=_dt(12),
                origin_unloaded_at=_dt(12),
                window_version=0,
            )
        )
        db.commit()
    finally:
        db.close()

    _fail_first_get_with_operational_error(monkeypatch)
    r = _manifest(
        client,
        "TR",
        [_item("C1", "08:00", "12:00"), _item("C2", "08:00", "13:00")],
    )
    assert r.status_code == 409, r.text  # 缺条 -> 重试 -> 与已落的 C1 同窗口重叠
    assert r.json()["error"]["code"] == "loading_overlap"
    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 1  # C2 未被补登
    assert d["revision"] == 0  # 直接插行不推进修订号；失败重试也不推进


# ----------------------------------------------------------- 携摘要确认登记


def _precheck_digest(client, trip, items, revision):
    r = client.post(
        f"/trips/{trip}/manifest/precheck",
        json={"expected_trip_revision": revision, "items": items},
    )
    assert r.status_code == 200, r.text
    return r.json()["digest"]


def _confirm(client, trip, items, digest):
    return client.post(
        f"/trips/{trip}/manifest/confirm",
        json={"digest": digest, "items": items},
    )


def test_confirm_precommit_failure_overlapping_old_window_rejected(
    client, monkeypatch
):
    """确认登记：旧 08:00-10:00 + 请求 08:00-12:00 提交前失败 -> 重试 -> 409。"""
    _setup_trip_cargo(client, cargos=("C1",))
    assert _manifest(client, "TR", [_item("C1", "08:00", "10:00")]).status_code == 201
    rev = _trip_detail(client, "TR")["revision"]
    items = [_item("C1", "08:00", "12:00")]
    digest = _precheck_digest(client, "TR", items, rev)

    _fail_first_get_with_operational_error(monkeypatch)
    r = _confirm(client, "TR", items, digest)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "loading_overlap"
    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 1
    assert d["loadings"][0]["unloaded_at"][11:16] == "10:00"
    assert d["revision"] == rev


def test_confirm_precommit_failure_stale_digest_still_rejected(client, monkeypatch):
    """本批未落库且预检后修订号已变（新增车载读数）：摘要过期仍拒绝。"""
    _setup_trip_cargo(client, cargos=("C1",))
    rev = _trip_detail(client, "TR")["revision"]
    items = [_item("C1", "08:00", "12:00")]
    digest = _precheck_digest(client, "TR", items, rev)

    # 预检后新增车载读数 -> 车次修订号 +1、读数版本变化 -> 摘要过期
    r = client.post(
        "/vehicle-readings",
        json={
            "reading_id": "vr1",
            "trip_id": "TR",
            "read_at": "2026-01-01T09:00:00+00:00",
            "temperature": "7.0",
        },
    )
    assert r.status_code == 201, r.text

    _fail_first_get_with_operational_error(monkeypatch)
    r = _confirm(client, "TR", items, digest)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "stale_precheck_digest"
    d = _trip_detail(client, "TR")
    assert d["loadings"] == []  # 不留任何装卸记录


def test_confirm_postcommit_failure_replays_without_double_revision(
    client, monkeypatch
):
    """确认登记 commit 落库后抛错：回放本次窗口；修订号只 +1、无重复登记。

    且旧摘要在首次确认成功时已随修订号失效：恢复后再用同摘要确认仍 409。
    """
    _setup_trip_cargo(client, cargos=("C1", "C2"))
    items = [_item("C1", "08:00", "12:00"), _item("C2", "08:00", "13:00")]
    digest = _precheck_digest(client, "TR", items, 0)
    state = _commit_raises_after_real_commit(monkeypatch)

    r = _confirm(client, "TR", items, digest)
    assert r.status_code == 201, r.text
    assert sorted(
        (it["cargo_id"], it["unloaded_at"][11:16]) for it in r.json()["items"]
    ) == [("C1", "12:00"), ("C2", "13:00")]
    assert state["commits"] == 1

    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 2
    assert d["revision"] == 1

    # 同一摘要重复确认（不再注入异常）：修订号已变 -> 摘要一次性，仍拒绝
    r2 = _confirm(client, "TR", items, digest)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "stale_precheck_digest"
    assert _trip_detail(client, "TR")["revision"] == 1


def test_confirm_precommit_failure_clean_db_retries_and_registers(
    client, monkeypatch
):
    """确认登记提交前失败、无冲突：重试原事务（含摘要校验）后成功。"""
    _setup_trip_cargo(client, cargos=("C1",))
    items = [_item("C1", "08:00", "12:00")]
    digest = _precheck_digest(client, "TR", items, 0)

    _fail_first_get_with_operational_error(monkeypatch)
    r = _confirm(client, "TR", items, digest)
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["unloaded_at"][11:16] == "12:00"
    d = _trip_detail(client, "TR")
    assert len(d["loadings"]) == 1 and d["revision"] == 1
