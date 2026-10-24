"""真实 E2E 在建立任何连接前，核验完整隔离配置。"""
import pytest


@pytest.fixture(autouse=True)
def isolated_e2e_resources(require_e2e):
    from app.rag_eval.ui_trial import validate_resources
    from app.shared.config import settings
    validate_resources({"MONGO_DB_NAME": settings.mongo.db_name, "CHUNKS_COLLECTION": settings.milvus.chunks_collection,
                        "ITEM_NAME_COLLECTION": settings.milvus.item_name_collection, "EVOLUTION_COLLECTION": settings.evolution.collection,
                        "MINIO_BUCKET_NAME": settings.minio.bucket_name, "MINIO_IMG_DIR": settings.minio.image_dir})
