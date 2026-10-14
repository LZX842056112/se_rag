"""MinIO 图片清理回归：只清理「本次未再引用」的对象。

回归背景：历史实现是 `remove_images(stem)` 先清空目录再逐张上传，上传失败会让该文档的
图片全部丢失。现改为「先上传（同名覆盖）、后清理残留」。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.shared.clients.minio_gateway import MinioGateway
from app.shared.config import settings


class _FakeMinio:
    def __init__(self, names: list[str]):
        self.names = list(names)
        self.removed: list[str] = []

    def list_objects(self, bucket_name: str, prefix: str, recursive: bool = True):
        return [SimpleNamespace(object_name=name) for name in self.names if name.startswith(prefix)]

    def remove_objects(self, bucket_name: str, delete_object_list: list):
        for obj in delete_object_list:
            name = getattr(obj, "name", None) or getattr(obj, "_name", None)
            self.removed.append(name)
            self.names = [item for item in self.names if item != name]
        return []


def _patch(monkeypatch, names: list[str]) -> _FakeMinio:
    fake = _FakeMinio(names)
    monkeypatch.setattr(MinioGateway, "minio_client", property(lambda self: fake))
    monkeypatch.setattr(settings.minio, "image_dir", "/upload-images")
    monkeypatch.setattr(settings.minio, "bucket_name", "kb-files")
    return fake


def test_only_unreferenced_objects_are_removed(monkeypatch):
    """保留本次引用的图片，删除残留的旧图片。"""
    fake = _patch(
        monkeypatch,
        [
            "upload-images/doc/a.png",
            "upload-images/doc/b.png",
            "upload-images/doc/removed.png",
        ],
    )

    removed = MinioGateway().remove_stale_images("doc", {"a.png", "b.png"})

    assert removed == 1
    assert fake.removed == ["upload-images/doc/removed.png"]
    assert "upload-images/doc/a.png" in fake.names, "本次引用的图片不得被删除"


def test_other_documents_are_untouched(monkeypatch):
    """前缀隔离：不得清理其它文档目录下的对象。"""
    fake = _patch(
        monkeypatch,
        ["upload-images/doc/a.png", "upload-images/other/a.png"],
    )

    removed = MinioGateway().remove_stale_images("doc", {"a.png"})

    assert removed == 0
    assert fake.removed == []
    assert "upload-images/other/a.png" in fake.names


def test_count_images_reports_objects_under_prefix(monkeypatch):
    """对账用的对象计数只统计当前文档前缀。"""
    _patch(monkeypatch, ["upload-images/doc/a.png", "upload-images/doc/b.png", "upload-images/x/c.png"])

    assert MinioGateway().count_images("doc") == 2
