"""Milvus 超时回归：构造参数与 per-RPC 参数必须两层都带。

回归背景：``MilvusClient(uri)`` 历史上不带任何超时。实测 pymilvus 2.6.10 的构造
``timeout`` 只覆盖建连等待（channel ready 与标识注册），**不覆盖 hybrid_search /
query 等业务 RPC**——后者缺省为 None 时会一直等到服务端响应或出错。因此两层都要显式
传参，否则 Milvus 网络挂起会让查询线程永久阻塞（三路召回里两路依赖它）。
"""
from __future__ import annotations

import grpc

from app.shared.clients import milvus_gateway as gw_module
from app.shared.config import settings
from app.shared.clients.milvus_gateway import hybrid_search

# offline_guard 会把模块级 get_milvus_client 换成抛错桩，先取回真实实现
_REAL_GET_MILVUS_CLIENT = gw_module.get_milvus_client


class _CapturingMilvusClient:
    """记录构造参数与调用参数的假 MilvusClient。"""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.calls: list[dict] = []

    def hybrid_search(self, **kwargs):
        self.calls.append(kwargs)
        return [[]]


def test_client_is_constructed_with_timeout(monkeypatch):
    """客户端单例必须带 timeout，覆盖建连等待。"""
    created: list[_CapturingMilvusClient] = []

    def _factory(**kwargs):
        client = _CapturingMilvusClient(**kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(gw_module, "MilvusClient", _factory)
    monkeypatch.setattr(gw_module, "_milvus_client", None)
    monkeypatch.setattr(gw_module, "get_milvus_client", _REAL_GET_MILVUS_CLIENT)

    client = gw_module.get_milvus_client()

    assert client is not None
    assert created, "应构造过客户端"
    assert created[0].init_kwargs.get("timeout") == settings.milvus.timeout_seconds


def test_hybrid_search_passes_per_call_timeout(monkeypatch):
    """业务 RPC 必须逐次传 timeout（构造参数覆盖不到）。"""
    fake = _CapturingMilvusClient()
    monkeypatch.setattr(gw_module, "MilvusClient", lambda **kw: fake)
    monkeypatch.setattr(gw_module, "_milvus_client", fake)

    hybrid_search(fake, "kb_chunks", reqs=[], ranker_weights=(0.6, 0.4), limit=5)

    assert fake.calls, "hybrid_search 未被调用"
    assert fake.calls[0].get("timeout") == settings.milvus.timeout_seconds


def test_hybrid_search_timeout_degrades_to_none():
    """RPC 超时由网关兜底为 None（调用方据此降级为空召回），不得外抛。"""
    class _Boom:
        def hybrid_search(self, **kwargs):
            raise grpc.RpcError("DEADLINE_EXCEEDED")

    assert hybrid_search(_Boom(), "kb_chunks", reqs=[], ranker_weights=(0.6, 0.4)) is None


def test_default_timeout_setting_is_sane():
    """超时默认值必须为正（对齐 Mongo 5s / LLM 60s 的既有梯队）。"""
    assert settings.milvus.timeout_seconds == 15
    assert settings.milvus.timeout_seconds > 0
