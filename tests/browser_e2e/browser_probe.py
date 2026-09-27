"""
浏览器真实环境全流程联调 —— HTTP / Mongo 旁证脚本（测试产物，非产品代码）。

用途：对浏览器 UI 难以触达的边界/异常分支做同环境接口级断言，并直查落库结果，
     为《浏览器真实环境全流程联调验证报告》提供可复用证据。

用法：
    python tests/browser_e2e/browser_probe.py http                  # 接口级断言
    python tests/browser_e2e/browser_probe.py http --session <sid>  # 追加 F1 字段断言
    python tests/browser_e2e/browser_probe.py mongo                 # 落库检查
    python tests/browser_e2e/browser_probe.py all                   # 全部

约定：
- 从项目根 .env 读取 EVOLUTION_ADMIN_TOKEN / MONGO_URL / MONGO_DB_NAME；token 不回显。
- 逐条输出 PASS/FAIL/INFO，末尾汇总；存在 FAIL 时退出码非 0。
- 所有写操作仅作用于"不存在 id"或"一次性 session"，不破坏真实业务数据。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"

QUERY_BASE = "http://127.0.0.1:8001"
IMPORT_BASE = "http://127.0.0.1:8000"

RESULTS: list[tuple[str, str, str]] = []


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
TOKEN = ENV.get("EVOLUTION_ADMIN_TOKEN", "")
MONGO_URL = ENV.get("MONGO_URL", "mongodb://127.0.0.1:27017")
MONGO_DB = ENV.get("MONGO_DB_NAME", "kb002")


def rec(status: str, case: str, evidence: str = "") -> None:
    RESULTS.append((status, case, evidence))
    print(f"[{status}] {case}" + (f"  ::  {evidence}" if evidence else ""))


def check(case: str, ok: bool, evidence: str = "") -> None:
    rec("PASS" if ok else "FAIL", case, evidence)


def info(case: str, evidence: str = "") -> None:
    rec("INFO", case, evidence)


def http(method: str, url: str, timeout: float = 90.0, **kw):
    import httpx

    t0 = time.time()
    try:
        r = httpx.request(method, url, timeout=timeout, **kw)
        return r, int((time.time() - t0) * 1000), None
    except Exception as e:  # noqa: BLE001
        return None, int((time.time() - t0) * 1000), str(e)


def detail(r) -> str:
    try:
        return str(r.json().get("detail"))
    except Exception:  # noqa: BLE001
        try:
            return r.text[:160]
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------- HTTP cases
def run_http() -> None:
    print("=== HTTP 接口级断言 ===")

    # ---- A6 空/缺 query ----
    r, ms, err = http("POST", f"{QUERY_BASE}/query", json={"is_stream": False})
    check("A6a 缺 query 字段 -> 422", r is not None and r.status_code == 422,
          f"status={r.status_code if r else 'ERR'} {err or ''} {ms}ms")

    r, ms, err = http("POST", f"{QUERY_BASE}/query", json={"query": "", "is_stream": False})
    check("A6b query 空串 -> 422", r is not None and r.status_code == 422,
          f"status={r.status_code if r else 'ERR'} {err or ''} {ms}ms")

    r, ms, err = http("POST", f"{QUERY_BASE}/query", json={"query": "   ", "is_stream": False}, timeout=45.0)
    if r is None:
        info("A6c 全空白 query", f"调用异常/超时（{err}） {ms}ms -> 记受限")
    elif r.status_code == 422:
        info("A6c 全空白 query", f"422 服务端拒绝 {ms}ms")
    elif r.status_code == 200:
        body = r.json()
        info("A6c 全空白 query",
             f"200（通过 min_length=1 校验并进入图，未按空白拒绝）answer={str(body.get('answer'))[:40]!r} {ms}ms")
    else:
        info("A6c 全空白 query", f"status={r.status_code} {detail(r)[:80]} {ms}ms")

    # ---- B4 feedback 缺 session_id ----
    r, ms, err = http("POST", f"{QUERY_BASE}/evolution/feedback", json={"query": "x", "thumbs": 1})
    check("B4 feedback 缺 session_id -> 422", r is not None and r.status_code == 422,
          f"status={r.status_code if r else 'ERR'} {err or ''} {ms}ms")

    # ---- D5 鉴权失败（写操作被拒，无副作用） ----
    r, ms, err = http("POST", f"{QUERY_BASE}/evolution/candidates/__nope__/approve",
                      json={"reason": ""}, headers={"X-Internal-Token": "wrong-token"})
    check("D5a approve 错 token -> 401", r is not None and r.status_code == 401,
          f"status={r.status_code if r else 'ERR'} {detail(r)[:60]} {ms}ms")

    r, ms, err = http("POST", f"{QUERY_BASE}/evolution/candidates/__nope__/approve", json={"reason": ""})
    check("D5b approve 无 token -> 401", r is not None and r.status_code == 401,
          f"status={r.status_code if r else 'ERR'} {detail(r)[:60]} {ms}ms")

    # ---- D6 不存在/不可审批（带合法 token，非破坏性） ----
    if not TOKEN:
        info("D6 跳过", "EVOLUTION_ADMIN_TOKEN 未配置")
    else:
        H = {"X-Internal-Token": TOKEN, "Content-Type": "application/json"}
        r, ms, err = http("POST", f"{QUERY_BASE}/evolution/candidates/__nope__/approve",
                          json={"reason": ""}, headers=H)
        check("D6a approve 不存在 id -> 404", r is not None and r.status_code == 404,
              f"status={r.status_code if r else 'ERR'} {detail(r)[:60]} {ms}ms")

        r, ms, err = http("POST", f"{QUERY_BASE}/evolution/candidates/__nope__/reject",
                          json={"reason": ""}, headers=H)
        check("D6b reject 不存在 id -> 404", r is not None and r.status_code == 404,
              f"status={r.status_code if r else 'ERR'} {detail(r)[:60]} {ms}ms")

        r, ms, err = http("POST", f"{QUERY_BASE}/evolution/candidates/__nope__/edit",
                          json={"faq_question": None, "faq_answer": None}, headers=H)
        check("D6c edit 空改动 -> 400", r is not None and r.status_code == 400,
              f"status={r.status_code if r else 'ERR'} {detail(r)[:60]} {ms}ms")

    # ---- E4 无文件 ----
    r, ms, err = http("POST", f"{IMPORT_BASE}/upload")
    check("E4 无文件 -> 422", r is not None and r.status_code == 422,
          f"status={r.status_code if r else 'ERR'} {detail(r)[:60]} {ms}ms")

    # ---- E5 非法文件名 ----
    r, ms, err = http("POST", f"{IMPORT_BASE}/upload",
                      files=[("files", ("..", b"x", "text/plain"))])
    check("E5 非法文件名('..') -> 422", r is not None and r.status_code == 422,
          f"status={r.status_code if r else 'ERR'} {detail(r)[:80]} {ms}ms")

    # ---- F2/F3 历史（一次性 session，不影响业务会话） ----
    sid = f"probe-{int(time.time())}"
    r, ms, err = http("GET", f"{QUERY_BASE}/history/{sid}")
    ok = r is not None and r.status_code == 200
    items = (r.json().get("items") if ok else []) or []
    check("F2a 新 session 历史为空 -> 200 空列表",
          ok and isinstance(items, list) and len(items) == 0,
          f"status={r.status_code if r else 'ERR'} items={len(items)} {ms}ms")

    r, ms, err = http("GET", f"{QUERY_BASE}/history/{sid}", params={"limit": 50})
    check("F2b limit=50 -> 200", r is not None and r.status_code == 200,
          f"status={r.status_code if r else 'ERR'} {ms}ms")

    r, ms, err = http("DELETE", f"{QUERY_BASE}/history/{sid}")
    ok = r is not None and r.status_code == 200
    body = r.json() if ok else {}
    check("F3a DELETE 历史 -> 200 含 deleted_count",
          ok and "deleted_count" in body,
          f"status={r.status_code if r else 'ERR'} deleted_count={body.get('deleted_count')} {ms}ms")

    r, ms, err = http("GET", f"{QUERY_BASE}/history/{sid}")
    ok = r is not None and r.status_code == 200
    items = (r.json().get("items") if ok else []) or []
    check("F3b 清空后再查为空", ok and len(items) == 0,
          f"status={r.status_code if r else 'ERR'} items={len(items)} {ms}ms")


def run_f1(sid: str) -> None:
    r, ms, err = http("GET", f"{QUERY_BASE}/history/{sid}", params={"limit": 10})
    ok = r is not None and r.status_code == 200
    items = (r.json().get("items") if ok else []) or []
    base_ok = bool(items) and all({"role", "text", "ts"}.issubset(i.keys()) for i in items)
    bot = [i for i in items if i.get("role") == "bot"]
    new_ok = bool(bot) and all(k in bot[0] for k in ("citations", "groundedness", "item_names"))
    check("F1 历史字段完整（role/text/ts + bot 的 citations/groundedness/item_names）",
          ok and base_ok and new_ok,
          f"status={r.status_code if r else 'ERR'} items={len(items)} bot={len(bot)} "
          f"bot_keys={sorted(bot[0].keys()) if bot else '-'} {ms}ms")


# --------------------------------------------------------------- Mongo cases
def run_mongo() -> None:
    print("=== Mongo 落库检查 ===")
    try:
        from pymongo import MongoClient
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "Mongo 依赖缺失", str(e))
        return

    try:
        cli = MongoClient(MONGO_URL, serverSelectionTimeoutMS=6000)
        cli.admin.command("ping")
        db = cli[MONGO_DB]
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "Mongo 连接失败", f"{MONGO_URL} -> {e}")
        return
    info("Mongo 连接成功", f"db={MONGO_DB}")

    # ---- fb_events（A7 / B1 / B2） ----
    fb = db["fb_events"]
    total = fb.count_documents({})
    up = fb.count_documents({"thumbs": 1})
    down = fb.count_documents({"thumbs": -1})
    info("A7/B1/B2 fb_events 计数", f"total={total} thumbs=+1:{up} -1:{down}")
    for d in fb.find({}, {"session_id": 1, "query": 1, "thumbs": 1, "item_names": 1,
                          "cited_chunk_ids": 1, "adopt": 1, "source": 1, "ts": 1}).sort("ts", -1).limit(5):
        d["_id"] = str(d["_id"])
        info("B 最新反馈样本", json.dumps(d, ensure_ascii=False, default=str)[:240])

    # ---- k_candidates（C1 / C3 / D2 / D3） ----
    kc = db["k_candidates"]
    dist = {s: kc.count_documents({"status": s})
            for s in ["draft", "active", "rejected", "candidate", "pending", "hold", "removed"]}
    info("D k_candidates 状态分布", json.dumps(dist, ensure_ascii=False))
    for d in kc.find({}, {"faq_question": 1, "status": 1, "item_names": 1, "source_refs": 1}).sort("ts", -1).limit(8):
        d["_id"] = str(d["_id"])
        info("D 候选样本", json.dumps(d, ensure_ascii=False, default=str)[:240])

    # ---- k_gaps（C1 / C2 / C3） ----
    kg = db["k_gaps"]
    gdist = {s: kg.count_documents({"status": s}) for s in ["pending", "candidate", "rejected"]}
    info("C k_gaps 状态分布", json.dumps(gdist, ensure_ascii=False))
    for d in kg.find({}, {"query": 1, "status": 1, "confidence": 1, "item_names": 1, "signals": 1}).sort("ts", -1).limit(5):
        d["_id"] = str(d["_id"])
        info("C 缺口样本", json.dumps(d, ensure_ascii=False, default=str)[:240])


def main() -> None:
    ap = argparse.ArgumentParser(description="浏览器联调 HTTP/Mongo 旁证脚本")
    ap.add_argument("mode", choices=["http", "mongo", "all"], nargs="?", default="all")
    ap.add_argument("--session", default=None, help="用于 F1 字段断言的历史会话 id")
    args = ap.parse_args()

    if args.mode in ("http", "all"):
        run_http()
        if args.session:
            run_f1(args.session)
    if args.mode in ("mongo", "all"):
        run_mongo()

    fails = [r for r in RESULTS if r[0] == "FAIL"]
    passes = sum(1 for r in RESULTS if r[0] == "PASS")
    infos = sum(1 for r in RESULTS if r[0] == "INFO")
    print(f"\n=== 汇总：PASS={passes} FAIL={len(fails)} INFO={infos} ===")
    for _, case, ev in fails:
        print(f"  FAIL: {case} :: {ev}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
