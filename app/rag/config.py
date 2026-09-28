"""导入端与查询端共享的业务常量。

历史问题：``SUPPORTED_IMAGE_EXTENSIONS`` 曾在 ``rag/import_/config.py``（set）与
``rag/query/config.py``（tuple）各定义一份，口径不同易漂移；现收敛到本文件。
"""
from __future__ import annotations

SUPPORTED_IMAGE_EXTENSIONS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")
