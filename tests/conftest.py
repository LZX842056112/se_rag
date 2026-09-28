"""pytest 共享配置。

- 把仓库根加入 ``sys.path``，保证测试无需安装包即可 import ``app``；
- 提供 ``e2e`` 用例的统一开关（``E2E_ENABLED=1`` 才真正执行）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def e2e_enabled() -> bool:
    """真机端到端开关。"""
    return os.getenv("E2E_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
def require_e2e():
    """需要真实外部服务的用例统一使用的守卫。"""
    if not e2e_enabled():
        pytest.skip("未设置 E2E_ENABLED=1，跳过真机端到端用例")


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """离线用例安全网：禁止真实建立外部连接。

    生产代码里的自进化参数注册表、主体名目录等增强能力在「外部服务不可用」时必须
    降级，本夹具把该场景固定下来：任何真实连接尝试都会抛错并被调用方吞掉。
    """
    if e2e_enabled():
        yield
        return

    from app.shared.clients import milvus_gateway as milvus_module
    from app.shared.clients import minio_gateway as minio_module
    from app.shared.clients import mongo as mongo_module

    def _blocked(*_args, **_kwargs):
        raise RuntimeError("离线用例禁止访问外部服务")

    monkeypatch.setattr(mongo_module, "get_mongo_client", _blocked)
    monkeypatch.setattr(milvus_module, "get_milvus_client", _blocked)
    monkeypatch.setattr(minio_module, "get_minio_client", _blocked)

    # 参数注册表带进程内缓存：清空并重置 TTL，保证每例都走「降级到常量」路径
    from app.evolution.tuning import param_registry

    monkeypatch.setattr(param_registry, "_LAST_LOAD", 0.0)
    param_registry._CACHE.clear()
    # 缺口扫描游标是进程内状态：不清会跨用例串味（前一个用例把游标推过了本例的事件）
    from app.evolution.gap import detector

    detector.reset_scan_cursor()
    yield
    param_registry._CACHE.clear()
    detector.reset_scan_cursor()
