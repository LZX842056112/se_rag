"""每请求的截止时间共享传输层，而不改动已缓存的模型客户端。"""
from __future__ import annotations

import asyncio

import pytest

from app.shared.models import llm


@pytest.fixture
def isolated_clients(monkeypatch):
    monkeypatch.setattr(llm, "_llm_client_cache", {})
    monkeypatch.setattr(llm.settings.llm, "api_key", "offline-test")
    monkeypatch.setattr(llm.settings.llm, "base_url", "https://offline.invalid/v1")
    monkeypatch.setattr(llm.settings.import_, "llm_timeout_seconds", 60.0)
    monkeypatch.setattr(llm.settings.import_, "llm_max_retries", 2)
    yield
    for client in llm._llm_client_cache.values():
        client.root_client.close()
        asyncio.run(client.root_async_client.close())


def test_request_deadlines_do_not_accumulate_cached_clients(isolated_clients):
    base = llm.get_llm_client(json_mode=True)
    requests = [llm.get_llm_client(json_mode=True, timeout_seconds=value / 10, max_retries=0)
                for value in range(1, 21)]
    assert len(llm._llm_client_cache) == 1
    assert base.root_client.timeout == 60.0
    assert base.root_client.max_retries == 2
    for index, client in enumerate(requests, 1):
        assert client.root_client.timeout == index / 10
        assert client.root_client.max_retries == 0
        assert client.root_client._client is base.root_client._client
        assert client.root_async_client._client is base.root_async_client._client


def test_request_deadline_copy_preserves_sync_async_and_stream_clients(isolated_clients):
    base = llm.get_llm_client()
    request = llm.get_llm_client(timeout_seconds=1.25, max_retries=0)
    assert request.client._client.timeout == 1.25
    assert request.async_client._client.timeout == 1.25
    assert request.root_async_client.max_retries == 0
    assert request.request_timeout == 1.25
    assert request.max_retries == 0
    assert base.request_timeout == 60.0
    assert base.max_retries == 2