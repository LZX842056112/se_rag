"""清理前在 5e83f29 固定的持久化文档契约。"""
import json
from pathlib import Path

import pytest

from app.evolution import models


@pytest.mark.parametrize("model_name", ["FeedbackEvent", "KnowledgeGap", "KnowledgeCandidate", "MetricSnapshot"])
def test_business_model_dump_preserves_stored_document(model_name):
    fixture = Path(__file__).parents[1] / "fixtures" / "evolution_documents.json"
    case = json.loads(fixture.read_text(encoding="utf-8"))[model_name]
    model = getattr(models, model_name)(**case["input"])
    assert model.model_dump() == case["document"]
