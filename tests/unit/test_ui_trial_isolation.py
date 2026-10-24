"""真实联调和 E2E 必须拒绝正式数据库、混用集合及默认图片路径。"""
import pytest

from app.rag_eval.ui_trial import resource_names, validate_resources


def test_trial_resource_names_are_isolated_and_valid_for_each_storage():
    names = resource_names("ui_test_20261024_1234abcd")
    validate_resources(names)
    assert names["MONGO_DB_NAME"] == "ui_test_20261024_1234abcd"
    assert names["CHUNKS_COLLECTION"] == "ui_test_20261024_1234abcd_chunks"
    assert names["MINIO_BUCKET_NAME"] == "ui-test-20261024-1234abcd"


def test_trial_images_are_readable_only_below_the_current_run_prefix():
    from app.rag_eval.ui_trial import image_policy
    names = resource_names('ui_test_20261024_1234abcd')
    policy = image_policy(names)
    assert policy['Statement'][0]['Action'] == ['s3:GetObject']
    assert policy['Statement'][0]['Resource'] == [
        'arn:aws:s3:::ui-test-20261024-1234abcd/ui_test_20261024_1234abcd/images/*']
    with pytest.raises(ValueError):
        image_policy({**names, 'MINIO_BUCKET_NAME':'production'})


@pytest.mark.parametrize("key,value", [
    ("MONGO_DB_NAME", "kb002"), ("CHUNKS_COLLECTION", "kb_chunks"),
    ("EVOLUTION_COLLECTION", "kb_evolution_items"), ("ITEM_NAME_COLLECTION", "kb_item_names"),
    ("MINIO_BUCKET_NAME", "production"), ("MINIO_IMG_DIR", "/upload-images"),
    ("CHUNKS_COLLECTION", "ui_test_20261024_1234abcd_items"),
    ("EVOLUTION_COLLECTION", "ui_test_20261024_ffffffff_faq"),
])
def test_mixed_or_production_resources_are_rejected_before_connection(key, value):
    names = resource_names("ui_test_20261024_1234abcd")
    names[key] = value
    with pytest.raises(ValueError, match="隔离"):
        validate_resources(names)


@pytest.mark.parametrize("identifier", ["kb002", "../ui_test_20261024_1234abcd", "ui_test_20261024_1234abcd;delete"])
def test_trial_id_cannot_redirect_resources_or_artifact_paths(identifier):
    with pytest.raises(ValueError):
        resource_names(identifier)


def test_collection_copy_retains_flat_milvus_hnsw_build_parameters():
    from app.rag_eval.ui_trial import index_options
    actual = index_options({"M": "64", "efConstruction": "100", "metric_type": "COSINE",
                            "index_type": "HNSW", "field_name": "dense_vector", "index_name": "dense_vector_index",
                            "total_rows": 126, "state": "Finished"})
    assert actual == {"field_name": "dense_vector", "index_name": "dense_vector_index", "index_type": "HNSW",
                      "metric_type": "COSINE", "params": {"M": 64, "efConstruction": 100}}
