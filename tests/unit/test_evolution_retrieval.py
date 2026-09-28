"""进化条目召回过滤：无事实的历史条目不得占用引用位。"""
from __future__ import annotations

from app.evolution.retrieval import _format


def _hit(evo_doc_id: str, answer: str, *, distance: float = 0.9) -> dict:
    return {
        "id": 1,
        "distance": distance,
        "entity": {
            "evo_doc_id": evo_doc_id,
            "faq_question": "问题",
            "faq_answer": answer,
            "item_name": "Brother HAK 180 烫金机",
            "status": "active",
        },
    }


def test_non_answer_entries_are_skipped():
    hits = [
        _hit("evo_bad", "根据参考片段，未提及该配对码，建议联系官方客服。"),
        _hit("evo_good", "配对码默认为 0000，可在设置-蓝牙中修改。"),
    ]
    items = _format(hits)
    assert [item["chunk_id"] for item in items] == ["evo_good"]


def test_good_entries_keep_shape():
    items = _format([_hit("evo_ok", "额定工作温度 0~40 摄氏度。")])
    assert items[0]["source"] == "evolution"
    assert items[0]["title"] == "问题"
    assert items[0]["content"].startswith("额定工作温度")
