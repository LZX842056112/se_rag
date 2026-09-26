import requests, uuid

CANDIDATES = [
    ("Q1-pet-烧", "HAK 180 烫印PET材质的温度、压力和保压时间设为多少最佳？"),
    ("Q1-no-product", "烫金机烫印PET材质时，温度和压力一般调到多少合适？"),
    ("Q2-cali", "烫金滚筒压力校准要什么工具？多久做一次？"),
    ("Q3-break", "烫印出现跳印断点，通常是什么原因？"),
]

BASE = "http://127.0.0.1:8001"
for tag, q in CANDIDATES:
    sid = "sess-diag-" + uuid.uuid4().hex[:6]
    try:
        r = requests.post(f"{BASE}/query",
                          json={"query": q, "session_id": sid, "is_stream": False}, timeout=120)
        d = r.json()
        print(f"[{tag}] HTTP {r.status_code} | cite={d.get('citations')} | ground={d.get('groundedness')} | ans={str(d.get('answer') or '')[:40]!r}")
        print(f"    sid={sid} q={q[:38]}")
    except Exception as e:
        print(f"[{tag}] ERR {e}")
        print(f"    sid={sid} q={q[:38]}")