"""系统路由：两个服务共用的健康检查。"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from fastapi import APIRouter, Request

from app.api.routers.pages import asset_version
from app.api.schema.query_schema import HealthResponseSchema
from app.shared.config import settings

router = APIRouter(prefix="/api", tags=["system"])
_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_SOURCE_SUFFIXES = {".py", ".prompt", ".html", ".css", ".js"}


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=_ROOT,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=3, check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _service_source_sha256() -> str | None:
    listed = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "app")
    paths = listed.split("\0") if listed is not None else (
        path.relative_to(_ROOT).as_posix() for path in (_ROOT / "app").rglob("*")
    )
    digest = hashlib.sha256()
    found = False
    try:
        for relative in sorted(set(paths)):
            path = _ROOT / relative
            if (
                not relative.startswith("app/") or relative.startswith("app/rag_eval/")
                or path.suffix not in _RUNTIME_SOURCE_SUFFIXES or path.name == "local_settings.py"
                or path.is_symlink() or not path.is_file()
            ):
                continue
            digest.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(path.read_bytes()).digest())
            found = True
    except OSError:
        return None
    return digest.hexdigest() if found else None


def query_provenance(runtime_backend: str) -> dict:
    config = settings.evaluation
    changes = _git("status", "--porcelain", "--untracked-files=normal", "--", "app", ":(exclude)app/rag_eval")
    return {
        "service_commit": config.service_commit or _git("rev-parse", "HEAD"),
        "service_dirty": bool(changes) if changes is not None else None,
        "service_source_sha256": _service_source_sha256(),
        "knowledge_base_version": config.knowledge_base_version or None,
        "worker_count": 1 if runtime_backend == "memory" else config.query_worker_count or None,
        "runtime_backend": runtime_backend,
        "models": {
            "answer": settings.llm.llm_model or None,
            "embedding": Path(settings.embedding.model_path).name or settings.embedding.model_name or None,
            "reranker": Path(settings.reranker.model_path).name or None,
            "manual_evidence_reranker": settings.llm.llm_model or None,
        },
    }


@router.get("/health", response_model=HealthResponseSchema, response_model_exclude_none=True)
def health(request: Request = None, include_evidence: bool = False) -> HealthResponseSchema:
    """健康检查：进程存活即返回 200；附带静态资源版本，供前端识别「页面已过期」。"""
    evaluation = getattr(request.app.state, "evaluation", None) if include_evidence and request else None
    return HealthResponseSchema(code=200, message="ok", asset_version=asset_version(), evaluation=evaluation)
