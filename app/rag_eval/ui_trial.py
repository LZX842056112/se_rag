"""隔离页面联调入口；正式配置仅用于读取并复制已有资料。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]


def resource_names(run: str) -> dict[str, str]:
    if not re.fullmatch(r"ui_test_\d{8}_[0-9a-f]{8}", run):
        raise ValueError("无效隔离联调标识")
    return {"MONGO_DB_NAME": run, "CHUNKS_COLLECTION": run + "_chunks",
            "ITEM_NAME_COLLECTION": run + "_items", "EVOLUTION_COLLECTION": run + "_faq",
            "MINIO_BUCKET_NAME": run.replace("_", "-"), "MINIO_IMG_DIR": run + "/images"}


def validate_resources(names: dict[str, str]) -> None:
    expected = resource_names(names.get("MONGO_DB_NAME", ""))
    if any(names.get(key) != value for key, value in expected.items()):
        raise ValueError("联调资源未完全隔离，拒绝连接或写入")


def image_policy(names: dict[str, str]) -> dict:
    """仅允许浏览器读取当前测试图片前缀；没有列举和写入权限。"""
    validate_resources(names)
    return {'Version': '2012-10-17', 'Statement': [{
        'Effect': 'Allow', 'Principal': {'AWS': ['*']}, 'Action': ['s3:GetObject'],
        'Resource': [f"arn:aws:s3:::{names['MINIO_BUCKET_NAME']}/{names['MINIO_IMG_DIR']}/*"],
    }]}


def folder(run: str) -> Path:
    resource_names(run)
    return ROOT / "output" / "ui_trial" / run


def save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _load_environment(overrides=None):
    from dotenv import dotenv_values
    # common.py 的 override=True 会覆盖启动进程的隔离配置。只在此独立入口先加载，
    # 然后禁用二次 dotenv 读取，正式 .env 不发生任何写入。
    for key, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            os.environ[key] = value
    os.environ.update({"PYTHON_DOTENV_DISABLED": "1", "LOG_FILE_ENABLE": "false"})
    os.environ.update(overrides or {})


def bootstrap(run: str, mode="legacy"):
    manifest = json.loads((folder(run) / "manifest.json").read_text(encoding="utf-8"))
    names = manifest["resources"]
    validate_resources(names)
    overrides = {**names, "QUERY_APP_PORT": "8011", "IMPORT_APP_PORT": "8010", "APP_HOST": "127.0.0.1",
                 "RAG_MODE": mode, "RAG_GRAY_PERCENT": "100" if mode == "agentic" else "0",
                 "EVOLUTION_ENABLED": "true", "EVOLUTION_RECALL_ENABLED": "true", "EVOLUTION_WRITE_ENABLED": "true",
                 "EVOLUTION_SCHEDULE_ENABLED": "true", "EVOLUTION_SCHEDULE_INTERVAL_MINUTES": "1",
                 "EVOLUTION_AUTO_TUNE_ENABLED": "false", "EVOLUTION_BACKTEST_ENABLED": "false",
                 "EVOLUTION_ADMIN_TOKEN": run + "-admin", "IMPORT_ADMIN_TOKEN": run + "-import",
                 "E2E_ENABLED": "1", "LOG_CONSOLE_ENABLE": "true", "LLM_MAX_RETRIES": "0"}
    _load_environment(overrides)
    if mode == 'agentic' and not os.environ.get('AGENT_TOKENIZER_PATH'):
        os.environ['AGENT_TOKENIZER_PATH'] = os.environ.get('BGE_M3_PATH', '')
    from app.shared.config import settings
    actual = {"MONGO_DB_NAME": settings.mongo.db_name, "CHUNKS_COLLECTION": settings.milvus.chunks_collection,
              "ITEM_NAME_COLLECTION": settings.milvus.item_name_collection, "EVOLUTION_COLLECTION": settings.evolution.collection,
              "MINIO_BUCKET_NAME": settings.minio.bucket_name, "MINIO_IMG_DIR": settings.minio.image_dir}
    validate_resources(actual)
    if actual != names:
        raise ValueError("配置加载后隔离资源发生变化")
    return settings, manifest


def _rows(client, name, fields=None):
    iterator = client.query_iterator(collection_name=name, filter="", output_fields=fields or ["*", "dense_vector", "sparse_vector"], batch_size=200, timeout=20)
    rows = []
    try:
        while batch := iterator.next():
            rows.extend(batch)
    finally:
        iterator.close()
    return rows


def _digest(rows):
    return hashlib.sha256(json.dumps(sorted(rows, key=lambda row: str(row.get("chunk_id", row.get("evo_doc_id", row.get("pk", ""))))),
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def index_options(entry: dict) -> dict:
    params = dict(entry.get("params") or {})
    for key in ("M", "efConstruction", "inverted_index_algo", "drop_ratio_build"):
        if key in entry:
            value = entry[key]
            params[key] = int(value) if key in {"M", "efConstruction"} else value
    return {"field_name": entry["field_name"], "index_name": entry["index_name"],
            "index_type": entry["index_type"], "metric_type": entry.get("metric_type", "IP"), "params": params}


def _clone_collection(client, source, target, rows):
    from pymilvus import DataType
    description = client.describe_collection(collection_name=source, timeout=20)
    if client.has_collection(collection_name=target, timeout=10):
        raise ValueError("隔离集合已经存在，拒绝覆盖：" + target)
    schema = client.create_schema(auto_id=description.get("auto_id", False), enable_dynamic_field=description.get("enable_dynamic_field", True))
    for field in description["fields"]:
        kwargs = dict(field.get("params") or {})
        kwargs.update(is_primary=field.get("is_primary", False))
        if field.get("nullable"):
            kwargs["nullable"] = True
        schema.add_field(field_name=field["name"], datatype=DataType(field["type"]), **kwargs)
    indexes = client.prepare_index_params()
    for name in client.list_indexes(collection_name=source, timeout=20):
        entry = client.describe_index(collection_name=source, index_name=name, timeout=20)
        indexes.add_index(**index_options(entry))
    client.create_collection(collection_name=target, schema=schema, index_params=indexes, consistency_level="Strong", timeout=30)
    primary = next(field["name"] for field in description["fields"] if field.get("is_primary"))
    copied = []
    for row in rows:
        row = dict(row)
        row.update(row.pop("$meta", {}) or {})
        if description.get("auto_id"):
            row.pop(primary, None)  # 主体目录内部自动主键不用于来源引用。
        copied.append(row)
    for start in range(0, len(copied), 100):
        client.insert(collection_name=target, data=copied[start:start+100], timeout=30)
    client.flush(collection_name=target, timeout=30)
    client.load_collection(collection_name=target, timeout=30)
    count = int(client.get_collection_stats(collection_name=target, timeout=20)["row_count"])
    if count != len(rows):
        raise ValueError(f"隔离复制数量不符：{target} {count}/{len(rows)}")
    return count


def prepare(run: str):
    from pymongo import MongoClient
    from pymilvus import MilvusClient
    from minio import Minio
    names = resource_names(run)
    validate_resources(names)
    path = folder(run) / "manifest.json"
    if path.exists():
        raise ValueError("本次标识已有记录，拒绝重新准备并覆盖测试数据")
    _load_environment({"LOG_CONSOLE_ENABLE": "false"})
    from app.shared.config import settings
    mongo = MongoClient(settings.mongo.url, serverSelectionTimeoutMS=5000)
    client = MilvusClient(uri=settings.milvus.url, timeout=20)
    source_db = mongo[settings.mongo.db_name]
    target_db = mongo[names["MONGO_DB_NAME"]]
    if target_db.list_collection_names():
        raise ValueError("目标数据库不是空数据库")
    source_names = [settings.milvus.chunks_collection, settings.milvus.item_name_collection, settings.evolution.collection]
    chunks = _rows(client, source_names[0])
    product = re.compile(r"hak\s*180|hl.?3040|er\s*2100", re.IGNORECASE)
    selected = [row for row in chunks if product.search(str(row.get("item_name", "")) + str(row.get("file_title", "")))]
    items = [row for row in _rows(client, source_names[1]) if product.search(str(row.get("item_name", "")))]
    candidates = list(source_db[settings.mongo.k_candidates_collection].find({"status": "active"}))
    from app.evolution.governance import is_retrievable
    approved = [doc for doc in candidates if is_retrievable(doc) and any(product.search(str(item)) for item in doc.get("item_names", []))]
    approved_ids = {doc["evo_doc_id"] for doc in approved}
    faq = [row for row in _rows(client, source_names[2]) if row.get("evo_doc_id") in approved_ids]
    doc_ids = list({row.get("doc_id") for row in selected if row.get("doc_id")})
    parents = list(source_db[settings.mongo.parent_chunks_collection].find({"doc_id": {"$in": doc_ids}}))
    manifest = {"run": run, "resources": names, "status": "preparing", "source_database": settings.mongo.db_name,
                "source_collections": source_names, "source_digest": _digest(selected), "source_rows": len(selected),
                "copied_products": sorted({row.get("item_name", "") for row in selected}),
                "approved_faq": len(approved), "excluded_active_faq": len(candidates)-len(approved),
                "answer_model": settings.llm.llm_model, "code_version": "working-tree", "mode": "legacy"}
    save(path, manifest)
    targets = [names["CHUNKS_COLLECTION"], names["ITEM_NAME_COLLECTION"], names["EVOLUTION_COLLECTION"]]
    counts = [_clone_collection(client, src, dst, rows) for src, dst, rows in zip(source_names, targets, [selected, items, faq])]
    if parents:
        target_db[settings.mongo.parent_chunks_collection].insert_many(parents)
    if approved:
        target_db[settings.mongo.k_candidates_collection].insert_many(approved)
    minio = Minio(settings.minio.endpoint, access_key=settings.minio.access_key, secret_key=settings.minio.secret_key, secure=settings.minio.secure)
    if minio.bucket_exists(names["MINIO_BUCKET_NAME"]):
        raise ValueError("目标图片桶已经存在，拒绝复用")
    # 复制的手册图片继续使用原来的只读 URL；新图片只开放当前测试前缀。
    minio.make_bucket(names["MINIO_BUCKET_NAME"])
    minio.set_bucket_policy(names['MINIO_BUCKET_NAME'], json.dumps(image_policy(names)))
    if _digest([row for row in _rows(client, source_names[0]) if product.search(str(row.get("item_name", "")) + str(row.get("file_title", "")))]) != manifest["source_digest"]:
        raise ValueError("复制期间源资料发生变化")
    manifest.update(status="ready", counts=dict(zip(targets, counts)), parent_count=len(parents))
    save(path, manifest)
    mongo.close()
    client.close()
    print(json.dumps(manifest, ensure_ascii=False))


def inspect_trial(run: str):
    settings, manifest = bootstrap(run)
    from app.shared.clients.mongo import get_mongo_db
    from app.shared.clients.milvus_gateway import milvus_gateway
    db = get_mongo_db()
    result = {"run": run, "mongo": {name: db[name].count_documents({}) for name in db.list_collection_names()},
              "milvus": {name: milvus_gateway.milvus_client.get_collection_stats(name, timeout=10)["row_count"]
                         for name in [settings.milvus.chunks_collection, settings.milvus.item_name_collection, settings.evolution.collection]}}
    save(folder(run) / "latest-inspection.json", result)
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "serve-query", "serve-import", "inspect", "cycle", "e2e", "configure-images"])
    parser.add_argument("--run", required=True)
    parser.add_argument("--mode", choices=["legacy", "agentic"], default="legacy")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.run)
        return
    settings, manifest = bootstrap(args.run, args.mode)
    if args.command.startswith("serve-"):
        import uvicorn
        query = args.command == "serve-query"
        if query:
            from app.rag.query import pipeline
            original = pipeline.query_app
            class RecordedGraph:
                def invoke(self, state):
                    result = original.invoke(state)
                    name = re.sub(r"[^A-Za-z0-9_-]", "_", state.get("turn_id") or "unknown")
                    try:
                        save(folder(args.run) / "snapshots" / (name + ".json"), result)
                    except OSError:
                        pass  # 诊断记录失败不改变真实查询结果。
                    return result
            pipeline.query_app = RecordedGraph()
        uvicorn.run("app.api.http." + ("query_server" if query else "import_server") + ":app",
                    host="127.0.0.1", port=8011 if query else 8010, workers=1)
    elif args.command == "inspect":
        inspect_trial(args.run)
    elif args.command == "cycle":
        from app.evolution.scheduler import run_evolution_cycle_once
        result = run_evolution_cycle_once(force=True)
        save(folder(args.run) / "cycle.json", result)
        print(json.dumps(result, ensure_ascii=False, default=str))
    elif args.command == 'configure-images':
        from app.shared.clients.minio_gateway import minio_gateway
        names = manifest['resources']
        minio_gateway.minio_client.set_bucket_policy(names['MINIO_BUCKET_NAME'], json.dumps(image_policy(names)))
        print('已设置本次隔离图片前缀的只读策略')
    else:
        import pytest
        os.environ["E2E_KEEP_DATA"] = "1"
        raise SystemExit(pytest.main(["-q", "-m", "e2e", "tests/e2e", "-s",
                                     "--junitxml=" + str(folder(args.run) / "e2e-regression.xml")]))


if __name__ == "__main__":
    main()
