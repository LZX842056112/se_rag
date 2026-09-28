"""MinIO 对象键与访问地址拼接测试（含 secure 两态，回归历史优先级缺陷）。"""
from __future__ import annotations

from app.shared.clients.minio_gateway import minio_gateway
from app.shared.config import settings


def test_object_key_has_no_leading_slash(monkeypatch):
    monkeypatch.setattr(settings.minio, "image_dir", "/upload-images")
    assert minio_gateway.object_key("doc", "a.png") == "upload-images/doc/a.png"
    assert minio_gateway.image_prefix("doc") == "upload-images/doc/"


def test_build_image_url_http(monkeypatch):
    monkeypatch.setattr(settings.minio, "image_dir", "/upload-images")
    monkeypatch.setattr(settings.minio, "endpoint", "10.0.0.1:9000")
    monkeypatch.setattr(settings.minio, "bucket_name", "kb")
    monkeypatch.setattr(settings.minio, "secure", False)
    assert minio_gateway.build_image_url("doc", "a.png") == "http://10.0.0.1:9000/kb/upload-images/doc/a.png"


def test_build_image_url_https(monkeypatch):
    """历史缺陷：secure=True 时运算符优先级错误，只返回 "https://"。"""
    monkeypatch.setattr(settings.minio, "image_dir", "upload-images")
    monkeypatch.setattr(settings.minio, "endpoint", "minio.example.com")
    monkeypatch.setattr(settings.minio, "bucket_name", "kb")
    monkeypatch.setattr(settings.minio, "secure", True)
    assert minio_gateway.build_image_url("doc", "a.png") == "https://minio.example.com/kb/upload-images/doc/a.png"
