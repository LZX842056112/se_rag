"""导入链路超时、并发与断点续传回归。

回归背景：MinerU 的申请/上传/轮询三处 `requests` 调用历史上都没有 `timeout`，
网络挂起会永久阻塞一个后台线程；图片摘要逐张串行调用视觉模型，几十张图时明显拖慢导入。
"""
from __future__ import annotations

import pytest

from app.rag.import_ import enrich_markdown_images as em
from app.rag.import_ import pdf_parse_service as svc


class _FakePdfPath:
    name = "manual.pdf"

    def read_bytes(self) -> bytes:
        return b"%PDF-1.4 fake"


def _patch_mineru_requests(monkeypatch, recorded: dict):
    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    def _post(**kwargs):
        recorded["post_timeout"] = kwargs.get("timeout")
        return _Resp({"code": 0, "data": {"batch_id": "b1", "file_urls": []}})

    def _get(url=None, headers=None, timeout=None):
        recorded["get_timeout"] = timeout
        return _Resp({
            "code": 0,
            "data": {"extract_result": [{"state": "done", "full_zip_url": "http://x/z.zip"}]},
        })

    monkeypatch.setattr(svc.requests, "post", _post)
    monkeypatch.setattr(svc.requests, "get", _get)
    monkeypatch.setattr(svc, "MINERU_POLL_INTERVAL_SECONDS", 0)


def test_upload_and_poll_passes_timeouts(monkeypatch):
    """申请上传地址与轮询都必须带上 timeout，否则网络挂起会永久阻塞。"""
    recorded: dict = {}
    _patch_mineru_requests(monkeypatch, recorded)

    zip_url = svc.upload_pdf_and_poll(_FakePdfPath())

    assert zip_url == "http://x/z.zip"
    assert recorded["post_timeout"] is not None, "申请上传地址缺少 timeout"
    assert recorded["get_timeout"] is not None, "轮询状态缺少 timeout"


def test_upload_pdf_passes_timeout(monkeypatch):
    """向预签名地址上传 PDF 也必须带 timeout。"""
    recorded: dict = {}
    _patch_mineru_requests(monkeypatch, recorded)

    class _Session:
        trust_env = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def put(self, url=None, data=None, timeout=None):
            recorded["put_timeout"] = timeout

            class _R:
                status_code = 200

            return _R()

    monkeypatch.setattr(svc.requests, "Session", _Session)

    class _Resp(_FakePdfPath):
        pass

    def _post(**kwargs):
        class _R:
            status_code = 200

            def json(self):
                return {"code": 0, "data": {"batch_id": "b1", "file_urls": ["http://put"]}}

        return _R()

    monkeypatch.setattr(svc.requests, "post", _post)
    svc.upload_pdf_and_poll(_FakePdfPath())

    assert recorded["put_timeout"] is not None, "上传 PDF 缺少 timeout"


def test_reuses_existing_markdown_when_newer(monkeypatch, tmp_path):
    """断点续传：已有比 PDF 更新的解析产物时跳过 MinerU。"""
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    md = tmp_path / "manual.md"
    md.write_text("# 手册", encoding="utf-8")
    md.touch()  # 确保 mtime 不早于 pdf

    def _must_not_call(*args, **kwargs):
        raise AssertionError("不应重新调用 MinerU 解析")

    monkeypatch.setattr(svc, "upload_pdf_and_poll", _must_not_call)
    monkeypatch.setattr(svc.settings.import_, "checkpoint_enabled", True)

    state = {"pdf_path": str(pdf), "local_dir": str(tmp_path)}
    result = svc.parse_pdf_to_markdown(state)

    assert result["md_path"] == str(md)


def test_checkpoint_disabled_still_calls_mineru(monkeypatch, tmp_path):
    """关闭断点续传时必须走正常解析路径。"""
    pdf = tmp_path / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    (tmp_path / "manual.md").write_text("# 旧产物", encoding="utf-8")

    called = {"n": 0}

    def _fake_poll(_pdf):
        called["n"] += 1
        return "http://x/z.zip"

    monkeypatch.setattr(svc, "upload_pdf_and_poll", _fake_poll)
    monkeypatch.setattr(svc, "download_and_extract_markdown", lambda *a, **k: tmp_path / "new.md")
    monkeypatch.setattr(svc.settings.import_, "checkpoint_enabled", False)

    state = {"pdf_path": str(pdf), "local_dir": str(tmp_path)}
    result = svc.parse_pdf_to_markdown(state)

    assert called["n"] == 1
    assert result["md_path"] == str(tmp_path / "new.md")


class _Chain:
    def __init__(self, fail_marker: str | None = None):
        self.calls: list[str] = []
        self.fail_marker = fail_marker

    def invoke(self, messages):
        text = messages[0].content[0]["text"]
        self.calls.append(text)
        if self.fail_marker and self.fail_marker in text:
            raise RuntimeError("视觉模型不可用")
        return "图片说明"


class _VisionClient:
    def __init__(self, chain: _Chain):
        self.chain = chain

    def __or__(self, _other):
        return self.chain


def _patch_vision(monkeypatch, chain: _Chain) -> None:
    monkeypatch.setattr(em.llm_providers, "vision_chat", lambda *a, **k: _VisionClient(chain))
    monkeypatch.setattr(em, "load_prompt", lambda name, **kwargs: f"PROMPT:{kwargs.get('image_content')}")
    monkeypatch.setattr(em.settings.import_, "image_summary_max_workers", 4)


def _items(count: int) -> list[tuple[str, str, tuple[str, str]]]:
    return [(f"img{i}.png", __file__, (f"ctx{i}", "下文")) for i in range(count)]


def test_summarize_images_returns_every_summary(monkeypatch):
    """并发路径必须为每张图片都产出说明。"""
    chain = _Chain()
    _patch_vision(monkeypatch, chain)

    summaries = em.summarize_images(_items(4), "doc")

    assert len(summaries) == 4
    assert len(chain.calls) == 4


def test_summarize_images_skips_failing_image(monkeypatch):
    """单张失败不阻断其余图片：回退串行时逐张容错。"""
    chain = _Chain(fail_marker="ctx2")
    _patch_vision(monkeypatch, chain)

    summaries = em.summarize_images(_items(4), "doc")

    assert len(summaries) == 3
    assert "img2.png" not in summaries


def test_summarize_images_handles_empty_input(monkeypatch):
    """没有图片时不得调用视觉模型。"""
    chain = _Chain()
    _patch_vision(monkeypatch, chain)

    assert em.summarize_images([], "doc") == {}
    assert chain.calls == []
