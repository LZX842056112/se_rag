"""
MinIO 对象存储访问层：客户端单例、对象命名与访问 URL 拼接。

对象键与访问 URL 的口径必须一致（历史上对象键带前导 ``/`` 而 URL 不带，
导致上传后的图片链接 404）。此处统一：对象键 = ``<image_dir>/<stem>/<file>``，
URL = ``<scheme>://<endpoint>/<bucket>/<key>``，均不含重复前导斜杠。
"""
from __future__ import annotations

import json
from typing import Optional

from minio import Minio
from minio.deleteobjects import DeleteObject

from app.shared.config import settings
from app.shared.runtime.logger import logger

_minio_client: Optional[Minio] = None


def _bucket_policy(bucket_name: str) -> str:
    """公开只读策略（图片需可被浏览器直接访问）。"""
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{bucket_name}/*"],
        }],
    })


def _ensure_bucket(client: Minio) -> None:
    """桶不存在则创建并设置公开只读策略（幂等）。"""
    bucket_name = settings.minio.bucket_name
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        client.set_bucket_policy(bucket_name, _bucket_policy(bucket_name))
        logger.info(f"MinIO 桶 {bucket_name} 已创建并设置访问策略")


def get_minio_client() -> Minio:
    """获取全局单例 MinIO 客户端（首次调用才建立连接）。"""
    global _minio_client
    if _minio_client is None:
        _minio_client = Minio(
            endpoint=settings.minio.endpoint,
            access_key=settings.minio.access_key,
            secret_key=settings.minio.secret_key,
            secure=settings.minio.secure,
        )
        _ensure_bucket(_minio_client)
        logger.info("MinIO 客户端初始化完成")
    return _minio_client


class MinioGateway:
    """对象存储访问入口：桶名、对象键与访问 URL 统一在此定义。"""

    @property
    def bucket_name(self) -> str:
        """目标桶名。"""
        return settings.minio.bucket_name

    @property
    def image_dir(self) -> str:
        """图片前缀（已去掉前导斜杠，保证对象键与 URL 一致）。"""
        return settings.minio.image_dir.strip("/")

    @property
    def minio_client(self) -> Minio:
        """全局单例客户端。"""
        return get_minio_client()

    def object_key(self, stem: str, image_name: str) -> str:
        """图片对象键：``<image_dir>/<stem>/<image_name>``。"""
        return f"{self.image_dir}/{stem}/{image_name}"

    def image_prefix(self, stem: str) -> str:
        """某文档图片的对象键前缀（用于列举/清理）。"""
        return f"{self.image_dir}/{stem}/"

    def build_image_url(self, stem: str, image_name: str) -> str:
        """拼接图片公网访问地址。"""
        scheme = "https" if settings.minio.secure else "http"
        return f"{scheme}://{settings.minio.endpoint}/{self.bucket_name}/{self.object_key(stem, image_name)}"

    def remove_images(self, stem: str) -> None:
        """删除某文档目录下的全部图片（幂等，失败只告警）。"""
        try:
            client = self.minio_client
            objects = client.list_objects(
                bucket_name=self.bucket_name,
                prefix=self.image_prefix(stem),
                recursive=True,
            )
            errors = client.remove_objects(
                self.bucket_name,
                delete_object_list=[DeleteObject(obj.object_name) for obj in objects],
            )
            for error in errors:
                logger.warning(f"MinIO 删除对象失败：{error}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"清理 MinIO 旧图片失败（忽略继续）：{exc}")

    def upload_image(self, stem: str, image_name: str, file_path: str, content_type: str) -> str:
        """上传单张图片，返回访问地址。"""
        self.minio_client.fput_object(
            bucket_name=self.bucket_name,
            object_name=self.object_key(stem, image_name),
            file_path=file_path,
            content_type=content_type,
        )
        return self.build_image_url(stem, image_name)


minio_gateway = MinioGateway()
