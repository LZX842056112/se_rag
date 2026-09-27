"""G1 RAG 批量评测入口（联调补充项，测试产物）。

用法：
    python tests/browser_e2e/run_g1_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.rag_eval import RagEvalTester  # noqa: E402


def main() -> None:
    t = RagEvalTester()
    ins = t.run_insert_test_data()
    print("[G1] insert:", json.dumps({k: v for k, v in ins.items() if k != "item_rows"}, ensure_ascii=False, default=str))
    res = t.run_eval()
    print("[G1] summary:", json.dumps(res.get("summary"), ensure_ascii=False, default=str))
    print("[G1] report_path:", res.get("report_path"))


if __name__ == "__main__":
    main()
