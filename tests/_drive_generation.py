"""驱动真实生成管线：scan_unresolved_feedbacks -> generate_candidate（真实 LLM 提炼）。
不伪造任何数据——消费上面 3 条真实浏览器业务的 adopt=False 信号。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.evolution.candidate.generator import generate_candidate
from app.evolution.repositories import get_evolution_mongo_tool

r = get_evolution_mongo_tool()
print("== 扫描前 fb_events =", r.fb_events.count_documents({}), " gaps =", r.k_gaps.count_documents({}))

gaps = scan_unresolved_feedbacks(batch=50)
print(f"scan 产出缺口 {len(gaps)} 条")
for g in gaps:
    print("  gap:", g.status, "| conf=", g.confidence, "|", g.transcript_slice)

print("\n== 逐条生成候选（真实 LLM）==")
created = 0
for g in gaps:
    if g.status != "candidate":
        print("  skip(非candidate):", g.transcript_slice)
        continue
    cand = generate_candidate(g.document(), context_docs=[])
    if cand:
        created += 1
        print(f"  -> draft: status={cand.status} | {cand.faq_question[:45]} | answers={len(cand.faq_answer)}字")

print(f"\n== 生成后 k_candidates 共 {r.k_candidates.count_documents({})} 条 ==")
for c in r.k_candidates.find({}).sort("ts", 1):
    print("  ", c.get("_id"), "|", c.get("status"), "|", c.get("faq_question")[:50])