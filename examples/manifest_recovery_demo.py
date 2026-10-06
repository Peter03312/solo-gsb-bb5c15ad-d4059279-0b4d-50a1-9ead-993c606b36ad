#!/usr/bin/env python3
"""车次装卸批量登记 —— 数据库操作异常后“恢复裁决”的可运行 HTTP 调用示例。

纯 curl 无法在服务端制造“commit 已落库但驱动报错 / 提交前死锁中止”，故本
脚本在**本进程内**用 uvicorn 起一个真实 HTTP 服务（内存 SQLite），通过标准库
``urllib`` 发送真实 HTTP 请求，并在服务端会话层**一次性注入**
``OperationalError``，逐条演示修复后的恢复契约：

1) 旧窗口 08:00–10:00 已存在，再请同票 08:00–12:00；本次事务**提交前**失败
   -> 恢复裁决重试原事务，按既有冲突语义返回 409 loading_overlap
   （修复前会把旧窗口 10:00 卸车伪装成成功结果回传）；
2) 全新批次、commit **已落库后**驱动报错 -> 探测到整批逐条一致，回放 201，
   不重复登记，车次修订号只 +1；
3) 携预检摘要确认登记：预检后插入车载读数使摘要过期，提交前失败 -> 重试重算
   摘要，仍返回 409 stale_precheck_digest，不留装卸记录；
4) 部分落库（库里只有本批 C1 一条、缺 C2）后提交前失败 -> 缺条不得伪装成功，
   重试按重叠返回 409 loading_overlap，缺的 C2 不被补登、修订号不推进。

运行：python3 examples/manifest_recovery_demo.py
不依赖第三方 HTTP 客户端；uvicorn / fastapi / sqlalchemy 取自 requirements.txt
（docker compose 镜像内已具备）。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request

# 允许直接以 `python3 examples/manifest_recovery_demo.py` 从仓库根目录运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TZ"] = "UTC"

import sqlalchemy.orm as orm  # noqa: E402
import uvicorn  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


# ------------------------------------------------------------ 内嵌 HTTP 服务


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # 等待服务就绪
    for _ in range(200):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            pass
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------ 极小 HTTP 客户端


def request(base: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")


def post(base, path, body):
    return request(base, "POST", path, body)


def get(base, path):
    return request(base, "GET", path)


def item(cid: str, lo: str, un: str) -> dict:
    return {
        "cargo_id": cid,
        "loaded_at": f"2026-01-01T{lo}:00+00:00",
        "unloaded_at": f"2026-01-01T{un}:00+00:00",
    }


def make_cargo(base, cid: str):
    status, _ = post(
        base,
        "/cargo",
        {
            "cargo_id": cid,
            "started_at": "2026-01-01T00:00:00+00:00",
            "temp_upper_limit": "4.0",
            "initial_holder": "北京冷库",
        },
    )
    assert status == 201, (cid, status)


def make_trip(base, trip: str):
    status, _ = post(base, "/trips", {"trip_id": trip, "vehicle_no": "京A-1"})
    assert status == 201, (trip, status)


# ------------------------------------------------------- 一次性异常注入


def inject_failure_before_commit():
    """让下一个请求事务的首个 Session.get 抛一次 OperationalError（提交前中止）。

    恢复探测只用 SELECT（scalars），不消耗 get；重试事务的首个 get 放行。
    返回还原函数，演示完一个场景后恢复未打补丁的 Session.get / commit。
    """
    state = {"armed": True}
    original_get = orm.Session.get

    def flaky_get(self, entity, *args, **kwargs):
        if state["armed"]:
            state["armed"] = False
            raise OperationalError(
                "SELECT ... (simulated lock/deadlock abort before commit)",
                {},
                Exception("simulated operational failure before commit"),
            )
        return original_get(self, entity, *args, **kwargs)

    orm.Session.get = flaky_get  # type: ignore[assignment]
    return lambda: setattr(orm.Session, "get", original_get)


def inject_failure_after_commit():
    """让下一次 Session.commit 在真实提交成功后抛一次 OperationalError。"""
    state = {"armed": True}
    original_commit = orm.Session.commit

    def flaky_commit(self):
        original_commit(self)
        if state["armed"]:
            state["armed"] = False
            raise OperationalError(
                "COMMIT ... (simulated post-commit driver failure)",
                {},
                Exception("simulated operational failure after commit"),
            )

    orm.Session.commit = flaky_commit  # type: ignore[assignment]
    return lambda: setattr(orm.Session, "commit", original_commit)


# ------------------------------------------------------------ 演示场景


def demo_overlapping_old_window_precommit_failure(base):
    print("\n[场景 1] 旧 08:00–10:00 已在车；同票再请 08:00–12:00，提交前失败")
    make_cargo(base, "CA1")
    make_trip(base, "TA1")
    status, body = post(base, "/trips/TA1/manifest", {"items": [item("CA1", "08", "10")]})
    print(f"  首次登记 08:00–10:00 -> HTTP {status}")
    rev_before = get(base, "/trips/TA1")[1]["revision"]

    restore = inject_failure_before_commit()
    status, body = post(base, "/trips/TA1/manifest", {"items": [item("CA1", "08", "12")]})
    restore()
    print(f"  提交前 OperationalError 后客户端原样重发 -> HTTP {status}")
    print(f"  error.code = {body['error']['code']}（修复前此处会 201 并回传 10:00 旧窗口）")

    detail = get(base, "/trips/TA1")[1]
    unload = [l["unloaded_at"][11:16] for l in detail["loadings"]]
    print(f"  库内窗口卸车时刻 = {unload}，车次修订号 {rev_before} -> {detail['revision']}（不推进）")
    assert status == 409 and body["error"]["code"] == "loading_overlap"
    assert unload == ["10:00"] and detail["revision"] == rev_before


def demo_postcommit_failure_replays_once(base):
    print("\n[场景 2] commit 已落库后驱动报错：完整批次逐条一致 -> 回放成功")
    make_cargo(base, "CB1")
    make_cargo(base, "CB2")
    make_trip(base, "TB1")

    restore = inject_failure_after_commit()
    status, body = post(
        base,
        "/trips/TB1/manifest",
        {"items": [item("CB1", "08", "12"), item("CB2", "08", "13")]},
    )
    restore()
    print(f"  commit 后 OperationalError -> HTTP {status}")
    unloads = sorted((i["cargo_id"], i["unloaded_at"][11:16]) for i in body["items"])
    print(f"  回放窗口（本次请求时刻）= {unloads}")

    detail = get(base, "/trips/TB1")[1]
    print(
        f"  loadings 数 = {len(detail['loadings'])}（无重复登记），"
        f"车次修订号 = {detail['revision']}（仅 +1）"
    )
    assert status == 201 and unloads == [("CB1", "12:00"), ("CB2", "13:00")]
    assert len(detail["loadings"]) == 2 and detail["revision"] == 1


def demo_confirm_stale_digest_precommit_failure(base):
    print("\n[场景 3] 携摘要确认：预检后新增读数致摘要过期，提交前失败仍拒绝")
    make_cargo(base, "CC1")
    make_trip(base, "TC1")
    status, pre = post(
        base,
        "/trips/TC1/manifest/precheck",
        {"expected_trip_revision": 0, "items": [item("CC1", "08", "12")]},
    )
    digest = pre["digest"]
    print(f"  预检（revision=0）-> digest = {digest[:22]}…")

    status, _ = post(
        base,
        "/vehicle-readings",
        {
            "reading_id": "vr-c1",
            "trip_id": "TC1",
            "read_at": "2026-01-01T09:00:00+00:00",
            "temperature": "7.0",
        },
    )
    print(f"  预检后补录 09:00 车载读数（修订号 +1，摘要过期）-> HTTP {status}")

    restore = inject_failure_before_commit()
    status, body = post(
        base,
        "/trips/TC1/manifest/confirm",
        {"digest": digest, "items": [item("CC1", "08", "12")]},
    )
    restore()
    print(f"  提交前 OperationalError 后重试 -> HTTP {status} error.code = {body['error']['code']}")
    loadings = get(base, "/trips/TC1")[1]["loadings"]
    print(f"  库内装卸记录 = {len(loadings)} 条（不留任何记录）")
    assert status == 409 and body["error"]["code"] == "stale_precheck_digest"
    assert loadings == []


def demo_partial_batch_precommit_failure(base):
    print("\n[场景 4] 部分落库（仅 C1 一条、缺 C2）后提交前失败：不得伪装成功")
    from datetime import datetime, timezone

    from app.models import Loading

    make_cargo(base, "CD1")
    make_cargo(base, "CD2")
    make_trip(base, "TD1")
    db = SessionLocal()
    db.add(
        Loading(
            trip_id="TD1",
            cargo_id="CD1",
            origin_loaded_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            loaded_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
            unloaded_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            origin_unloaded_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            window_version=0,
        )
    )
    db.commit()
    db.close()
    print("  预置：库中只有本批 CD1 08:00–12:00 一条（CD2 缺失，模拟部分落库）")

    restore = inject_failure_before_commit()
    status, body = post(
        base,
        "/trips/TD1/manifest",
        {"items": [item("CD1", "08", "12"), item("CD2", "08", "13")]},
    )
    restore()
    print(f"  提交前 OperationalError 后重试 -> HTTP {status} error.code = {body['error']['code']}")
    detail = get(base, "/trips/TD1")[1]
    print(
        f"  loadings 数 = {len(detail['loadings'])}（CD2 未被补登），"
        f"车次修订号 = {detail['revision']}（不推进）"
    )
    assert status == 409 and body["error"]["code"] == "loading_overlap"
    assert len(detail["loadings"]) == 1 and detail["revision"] == 0


def main():
    base = _start_server()
    print(f"内嵌 HTTP 服务已启动：{base}（内存 SQLite）")
    demo_overlapping_old_window_precommit_failure(base)
    demo_postcommit_failure_replays_once(base)
    demo_confirm_stale_digest_precommit_failure(base)
    demo_partial_batch_precommit_failure(base)
    print("\n全部恢复裁决场景符合预期 ✔")


if __name__ == "__main__":
    main()
