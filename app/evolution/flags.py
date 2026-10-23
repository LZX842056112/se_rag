"""独立的召回与写入开关，并保留传统默认值。"""
from app.shared.config import settings


def recall_enabled() -> bool:
    value = getattr(settings.evolution, "recall_enabled", None)
    return bool(settings.evolution.enabled if value is None else value)


def writes_enabled() -> bool:
    value = getattr(settings.evolution, "write_enabled", None)
    return bool(settings.evolution.enabled if value is None else value)