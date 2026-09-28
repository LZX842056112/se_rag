"""Mongo 访问层测试：惰性连接、集合名来源与「不得自死锁」。"""
from __future__ import annotations

import threading

import mongomock

from app.shared.clients import mongo as mongo_module
# 收集期先取到真实实现：offline_guard 会在用例内把模块属性替换成抛错版本
from app.shared.clients.mongo import get_mongo_client as _real_get_mongo_client
from app.shared.config import settings


def _reset(monkeypatch):
    """把客户端构造替换为 mongomock，并恢复真实的 get_mongo_client（用于检验取锁路径）。"""
    monkeypatch.setattr(mongo_module, "_client", None)
    monkeypatch.setattr(mongo_module, "_db", None)
    monkeypatch.setattr(mongo_module, "_indexes_ready", False)
    monkeypatch.setattr(mongo_module, "get_mongo_client", _real_get_mongo_client)
    monkeypatch.setattr(mongo_module, "MongoClient", lambda *args, **kwargs: mongomock.MongoClient())


def test_get_collection_does_not_deadlock(monkeypatch):
    """回归：get_collection→get_mongo_db→get_mongo_client 嵌套取锁不得自死锁。"""
    monkeypatch.setattr(settings.mongo, "url", "mongodb://localhost:27017")
    _reset(monkeypatch)

    result: dict = {}

    def work() -> None:
        result["collection"] = mongo_module.get_collection("chat_message")

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "get_collection 发生死锁（锁不可重入）"
    assert result["collection"].name == "chat_message"


def test_client_is_lazy_and_singleton(monkeypatch):
    monkeypatch.setattr(settings.mongo, "url", "mongodb://localhost:27017")
    _reset(monkeypatch)

    first = mongo_module.get_mongo_client()
    second = mongo_module.get_mongo_client()
    assert first is second
