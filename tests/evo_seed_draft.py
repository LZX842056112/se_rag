# -*- coding: utf-8 -*-
"""一次性脚本：向 k_candidates 注入 3 条可控 draft 候选，供审批真实场景浏览器联调。幂等。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evolution.repositories import get_evolution_mongo_tool
from app.evolution.models import KnowledgeCandidate

SEED = [
    dict(
        faq_question="HAK 180 烫金机 烫印温度与压力调试参数",
        faq_answer="HAK 180 烫金机烫印温度建议 100~130°C，烫印压力 3.0~4.0MPa。温度过高易导致金箔脱落或烫花，压力不足会转印不实。开机后建议先空转 5 分钟预热，再按承印物材质微调。",
        source_refs=["seed://hak180_hot_foil"],
        item_names=["HAK180"],
    ),
    dict(
        faq_question="染料墨水 UV 光油 固化时间与残留气味处理",
        faq_answer="UV 光油固化时间取决于灯管功率与车速，通常 0.5~2 秒；UV 灯需定期检查能量衰减。残留气味多来自未完全固化，可加大 UV 能量、降低车速并保持通风，必要时延长光照距离。",
        source_refs=["seed://uvtopcoat_cure"],
        item_names=["UV光油"],
    ),
    dict(
        faq_question="印刷机 纠偏张力 跑偏报警 处理方法",
        faq_answer="跑偏报警应先检查纠偏传感器对位与光电信号，其次核对收放卷张力设定是否过小或波动，张力宜按材料克重分段设定。排除机械导辊歪斜后，重新校零并低速测试，确认进料居中再提速。",
        source_refs=["seed://web_guide"],
        item_names=["印刷机"],
    ),
]

repo = get_evolution_mongo_tool()


def main():
    inserted = 0
    skipped = 0
    existing = set(d["faq_question"] for d in repo.k_candidates.find({}, {"faq_question": 1}))
    for s in SEED:
        if s["faq_question"] in existing:
            print(f"[skip] 已存在: {s['faq_question']}")
            skipped += 1
            continue
        cand = KnowledgeCandidate(
            faq_question=s["faq_question"],
            faq_answer=s["faq_answer"],
            source_refs=s["source_refs"],
            item_names=s["item_names"],
            status="draft",
            ts=time.time(),
        )
        repo.k_candidates.insert_one(cand.document())
        print(f"[insert] draft: {s['faq_question']}")
        inserted += 1
    print(f"done: inserted={inserted} skipped={skipped}")


if __name__ == "__main__":
    main()