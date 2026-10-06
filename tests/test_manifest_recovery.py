"""车次装卸批量登记 / 携摘要确认登记在数据库操作异常后的恢复裁决测试。

模拟两类 ``OperationalError``（SQLite 锁等待耗尽 / PG 死锁中止的抽象）：

* **提交前失败**：事务在落库前被中止（首次 ``Session.get`` 抛错）。
  恢复裁决不得仅凭（车次、货物、首次装车时刻）命中旧窗口就伪装成功——
  必须整批每一条原始装卸时刻（首次装车 + 首次卸车时刻）都与请求一致才
  能按既有结果返回；否则重放原事务校验：旧窗口重叠按 ``loading_overlap``
  拒绝、过期预检摘要按 ``stale_precheck_digest`` 拒绝、部分落库或其他
  同刻窗口均不得伪装成功；
* **提交后失败**：commit 实际已落库但驱动报错（``Session.commit`` 真实
  提交后抛错）。恢复裁决确认整批已原样落库后按既有结果返回——不重复
  登记、不再推进车次修订号。
"""
import sqlalchemy.orm as orm
from sqlalchemy.exc import OperationalError


def post_trip(client, trip_id="T1"):
    r = client.post("/trips", json={"trip_id": trip_id, "vehicle_no": "京A-001"})
    assert r.status_code == 201, r.text


def post_manifest(client, trip_id, items):
    return client.post(f"/trips/{trip_id}/manifest", json={"items": items})


def post_precheck(client, trip_id, items, expected):
    return client.post(
        f"/trips/{trip_id}/manifest/precheck",
        json={"expected_trip_revision": expected, "items": items},
    )


def post_confirm(client, trip_id, items, digest):
    return client.post(
        f"/trips/{trip_id}/manifest/confirm",
        json={"digest": digest, "items": items},
    )


def post_reading(client, rid, trip_id, at, temp):
    return client.post(
        "/vehicle-readings",
        json={
            "reading_id": rid,
            "trip_id": trip_id,
            "read_at": at,
            "temperature": str(temp),
        },
    )


W_OLD = {  # 既有窗口：C1 08:00-10:00
    "cargo_id": "C1",
    "loaded_at": "2026-01-01T08:00:00+00:00",
    "unloaded_at": "2026-01-01T10:00:00+00:00",
}
W_NEW = {  # 同票同装车时刻、不同卸车时刻：C1 08:00-12:00
    "cargo_id": "C1",
    "loaded_at": "2026-01-01T08:00:00+00:00",
    "unloaded_at": "2026-01-01T12:00:00+00:00",
}
W_C2 = {  # 另一票货物同刻窗口：C2 08:00-10:00
    "cargo_id": "C2",
    "loaded_at": "2026-01-01T08:00:00+00:00",
    "unloaded_at": "2026-01-01T10:00:00+00:00",
}
W_LATE = {  # 不重叠窗口：C1 12:00-14:00
    "cargo_id": "C1",
    "loaded_at": "2026-01-01T12:00:00+00:00",
    "unloaded_at": "2026-01-01T14:00:00+00:00",
}


def _fail_first_tx_get_with_operational_error(monkeypatch):
    """让下一个请求事务的首次 ``Session.get`` 抛 OperationalError。

    模拟事务**提交前**被中止（锁等待耗尽 / 死锁）：登记事务回滚、进入
    恢复裁决分支；恢复查询与可能的重试事务一律放行。
    """
    state = {"armed": True}
    real_get = orm.Session.get

    def flaky_get(self, entity, *args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise OperationalError(
                "SELECT ... (simulated pre-commit abort)", {},
                Exception("simulated operational failure"),
            )
        return real_get(self, entity, *args, **kwargs)

    monkeypatch.setattr(orm.Session, "get", flaky_get)
    return state


def _fail_first_commit_after_success_with_operational_error(monkeypatch):
    """让下一次 ``Session.commit`` 在**真实提交落库后**抛 OperationalError。

    模拟 commit 已生效（含车次修订号 +1）但驱动向客户端报错：恢复裁决
    应确认整批已原样落库并按既有结果返回，不得重复登记或再推进修订号。
    """
    state = {"armed": True}
    real_commit = orm.Session.commit

    def flaky_commit(self, *args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            real_commit(self, *args, **kwargs)
            raise OperationalError(
                "COMMIT ... (simulated post-commit driver error)", {},
                Exception("simulated post-commit failure"),
            )
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(orm.Session, "commit", flaky_commit)
    return state


def _trip_view(client, trip_id="T1"):
    return client.get(f"/trips/{trip_id}").json()


# ----------------------------------------------------------- 提交前失败：不得伪装成功


def test_recovery_same_loaded_at_other_window_not_success(client, make_cargo, monkeypatch):
    """已存在 08:00-10:00 窗口时，同票 08:00-12:00 请求在提交前失败：

    恢复裁决不得把旧窗口（同车次同票同装车时刻、卸车时刻不同）当作本次
    成功结果返回；重放原事务校验后按既有冲突语义拒绝（409 loading_overlap），
    旧窗口与车次修订号均不变。
    """
    make_cargo("C1")
    post_trip(client)
    assert post_manifest(client, "T1", [W_OLD]).status_code == 201
    assert _trip_view(client)["revision"] == 1

    _fail_first_tx_get_with_operational_error(monkeypatch)
    r = post_manifest(client, "T1", [W_NEW])
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "loading_overlap"

    view = _trip_view(client)
    assert view["revision"] == 1  # 未推进修订号
    assert len(view["loadings"]) == 1
    only = view["loadings"][0]
    assert only["loaded_at"] == "2026-01-01T08:00:00Z"
    assert only["unloaded_at"] == "2026-01-01T10:00:00Z"  # 旧窗口原样保留


def test_recovery_partial_batch_presence_not_success(client, make_cargo, monkeypatch):
    """部分窗口已存在（C1 08:00-10:00）而整批未落库（C2 缺）时提交前失败：

    部分落库不得伪装成整批成功；重放原事务校验后按重叠冲突拒绝，
    C2 不落库、修订号不变。
    """
    make_cargo("C1")
    make_cargo("C2")
    post_trip(client)
    assert post_manifest(client, "T1", [W_OLD]).status_code == 201

    _fail_first_tx_get_with_operational_error(monkeypatch)
    r = post_manifest(client, "T1", [W_OLD, W_C2])
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "loading_overlap"

    view = _trip_view(client)
    assert view["revision"] == 1
    assert [l["cargo_id"] for l in view["loadings"]] == ["C1"]


def test_recovery_confirm_same_loaded_at_other_window_not_success(
    client, make_cargo, monkeypatch
):
    """携预检摘要的确认登记：同票 08:00-12:00 与既有 08:00-10:00 重叠，
    提交前失败时不得回传旧窗口作为成功结果；摘要有效但重叠仍 409。"""
    make_cargo("C1")
    post_trip(client)
    assert post_manifest(client, "T1", [W_OLD]).status_code == 201
    revision = _trip_view(client)["revision"]
    digest = post_precheck(client, "T1", [W_NEW], expected=revision).json()["digest"]

    _fail_first_tx_get_with_operational_error(monkeypatch)
    r = post_confirm(client, "T1", [W_NEW], digest)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "loading_overlap"

    view = _trip_view(client)
    assert view["revision"] == revision
    assert len(view["loadings"]) == 1
    assert view["loadings"][0]["unloaded_at"] == "2026-01-01T10:00:00Z"


def test_recovery_confirm_stale_digest_still_rejected(
    client, make_cargo, monkeypatch
):
    """携过期摘要的确认登记在提交前失败：即使存在同装车时刻旧窗口，
    也不得伪装成功；重放校验后摘要过期仍按 409 stale_precheck_digest 拒绝。"""
    make_cargo("C1")
    post_trip(client)
    assert post_manifest(client, "T1", [W_OLD]).status_code == 201
    revision = _trip_view(client)["revision"]
    digest = post_precheck(client, "T1", [W_NEW], expected=revision).json()["digest"]
    # 预检后写入一笔车载读数 -> 车次修订号 +1，摘要过期
    assert post_reading(
        client, "vr-stale", "T1", "2026-01-01T09:00:00+00:00", "3.0"
    ).status_code == 201

    _fail_first_tx_get_with_operational_error(monkeypatch)
    r = post_confirm(client, "T1", [W_NEW], digest)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "stale_precheck_digest"

    view = _trip_view(client)
    assert view["revision"] == revision + 1  # 只有读数推进的一次
    assert len(view["loadings"]) == 1
    assert view["loadings"][0]["unloaded_at"] == "2026-01-01T10:00:00Z"


# ----------------------------------------------------------- 提交后失败：按既有结果返回、不重复生效


def test_recovery_post_commit_error_returns_batch_without_double_effects(
    client, make_cargo, monkeypatch
):
    """commit 已落库（含修订号 +1）但驱动报错：恢复裁决确认整批原始装卸
    时刻与请求一致后按成功返回——不重复登记、不再推进车次修订号。"""
    make_cargo("C1")
    make_cargo("C2")
    post_trip(client)

    _fail_first_commit_after_success_with_operational_error(monkeypatch)
    r = post_manifest(client, "T1", [W_OLD, W_C2])
    assert r.status_code == 201, r.text
    items = {(i["cargo_id"], i["loaded_at"], i["unloaded_at"]) for i in r.json()["items"]}
    assert items == {
        ("C1", "2026-01-01T08:00:00Z", "2026-01-01T10:00:00Z"),
        ("C2", "2026-01-01T08:00:00Z", "2026-01-01T10:00:00Z"),
    }

    view = _trip_view(client)
    assert view["revision"] == 1  # 只推进一次
    assert sorted(l["cargo_id"] for l in view["loadings"]) == ["C1", "C2"]


def test_recovery_confirm_post_commit_error_no_double_effects(
    client, make_cargo, monkeypatch
):
    """确认登记 commit 落库后驱动报错：按既有结果返回 201，
    不重复登记、修订号只 +1；既有旧窗口（同票同装车时刻）不受影响。"""
    make_cargo("C1")
    post_trip(client)
    assert post_manifest(client, "T1", [W_OLD]).status_code == 201
    revision = _trip_view(client)["revision"]
    digest = post_precheck(client, "T1", [W_LATE], expected=revision).json()["digest"]

    _fail_first_commit_after_success_with_operational_error(monkeypatch)
    r = post_confirm(client, "T1", [W_LATE], digest)
    assert r.status_code == 201, r.text
    item = r.json()["items"][0]
    assert item["loaded_at"] == "2026-01-01T12:00:00Z"
    assert item["unloaded_at"] == "2026-01-01T14:00:00Z"

    view = _trip_view(client)
    assert view["revision"] == revision + 1  # 只推进一次
    assert len(view["loadings"]) == 2
    windows = {(l["loaded_at"], l["unloaded_at"]) for l in view["loadings"]}
    assert windows == {
        ("2026-01-01T08:00:00Z", "2026-01-01T10:00:00Z"),  # 旧窗口原样保留
        ("2026-01-01T12:00:00Z", "2026-01-01T14:00:00Z"),  # 本次确认落库
    }
