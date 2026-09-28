"""外部系统访问层（Milvus / MongoDB / MinIO）。

约定：本包内每个文件对应一个外部系统，只暴露连接单例与低层读写能力；业务模块请直接
从具体模块导入（例如 ``from app.shared.clients.mongo import get_collection``），不再通过
本文件做二次转发，避免出现「包装层的包装层」。
"""
