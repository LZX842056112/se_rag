"""PDF 解析服务：调用 MinerU 云服务把 PDF 转为 Markdown 并解压落地。

流程：校验路径 → 申请上传地址 → 上传 PDF → 轮询解析状态 → 下载 zip → 解压 → 重命名。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import requests

from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.config import (
    MINERU_DOWNLOAD_TIMEOUT_SECONDS,
    MINERU_MODEL_VERSION,
    MINERU_POLL_INTERVAL_SECONDS,
    MINERU_POLL_TIMEOUT_SECONDS,
    PDF_PARSE_SERVICE_LOCAL_DIR,
)
from app.shared.config import settings
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.paths import PROJECT_ROOT
from app.shared.utils.require import require_state_str


def _headers() -> dict[str, str]:
    """MinerU 请求头（Bearer Token）。"""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.mineru.api_key}",
    }


@step_log("validate_pdf_paths")
def validate_pdf_paths(state: ImportGraphState) -> tuple[Path, Path]:
    """校验 ``pdf_path`` 是否存在，并确保 ``local_dir`` 目录可用。"""
    pdf_path = require_state_str(state, "pdf_path")
    local_dir = state.get("local_dir")
    if not local_dir:
        local_dir = str(PROJECT_ROOT / PDF_PARSE_SERVICE_LOCAL_DIR)
        state["local_dir"] = local_dir
        logger.warning(f"local_dir 为空，使用默认目录：{local_dir}")

    pdf_path_obj = Path(pdf_path)
    if not pdf_path_obj.exists():
        logger.error(f"pdf_path 指向的文件不存在：{pdf_path_obj}")
        raise FileNotFoundError(f"pdf_path 指向的文件不存在：{pdf_path_obj}")

    local_dir_obj = Path(local_dir)
    if not local_dir_obj.is_dir():
        local_dir_obj.mkdir(parents=True, exist_ok=True)
    return pdf_path_obj, local_dir_obj


@step_log("upload_pdf_and_poll")
def upload_pdf_and_poll(pdf_path_obj: Path) -> str:
    """上传 PDF 到 MinerU 并轮询解析结果，返回结果 zip 的下载地址。"""
    base_url = settings.mineru.base_url
    # 1. 申请批量上传的预签名地址
    response = requests.post(
        url=f"{base_url}/file-urls/batch",
        headers=_headers(),
        json={"files": [{"name": pdf_path_obj.name}], "model_version": MINERU_MODEL_VERSION},
    )
    if response.status_code != 200:
        raise RuntimeError(f"申请 MinerU 解析失败，HTTP 状态码：{response.status_code}")
    response_dict = response.json()
    if response_dict.get("code", -1) != 0:
        raise RuntimeError(
            f"申请 MinerU 解析失败，业务状态码：{response_dict.get('code', -1)}，"
            f"原因：{response_dict.get('msg')}"
        )

    data = response_dict.get("data", {})
    batch_id = data.get("batch_id")
    file_upload_urls = data.get("file_urls", [])
    if not batch_id:
        raise ValueError("申请 MinerU 解析失败：返回的 batch_id 为空")
    logger.info(f"MinerU 上传申请完成：batch_id={batch_id}")

    # 2. 向预签名地址上传 PDF
    # 预签名地址对请求头敏感，故使用 trust_env=False 的会话，避免系统代理注入额外头
    if file_upload_urls:
        with requests.Session() as session:
            session.trust_env = False
            upload_response = session.put(url=file_upload_urls[0], data=pdf_path_obj.read_bytes())
        if upload_response.status_code != 200:
            raise RuntimeError(f"上传 PDF 到 MinerU 失败，HTTP 状态码：{upload_response.status_code}")

    # 3. 轮询解析状态
    result_url = f"{base_url}/extract-results/batch/{batch_id}"
    start_time = time.time()
    while True:
        if time.time() - start_time >= MINERU_POLL_TIMEOUT_SECONDS:
            raise TimeoutError(f"MinerU 解析超时（{MINERU_POLL_TIMEOUT_SECONDS}s），batch_id={batch_id}")

        try:
            poll_result = requests.get(result_url, headers=_headers())
        except Exception as exc:  # noqa: BLE001 - 网络抖动按重试处理
            logger.warning(f"MinerU 轮询网络异常（稍后重试）：{exc}")
            time.sleep(MINERU_POLL_INTERVAL_SECONDS)
            continue

        if poll_result.status_code != 200:
            if 500 <= poll_result.status_code < 600:
                logger.warning(f"MinerU 服务端 {poll_result.status_code}，稍后重试")
                time.sleep(MINERU_POLL_INTERVAL_SECONDS)
                continue
            raise RuntimeError(f"获取 MinerU 解析结果失败，HTTP 状态码：{poll_result.status_code}")

        poll_dict = poll_result.json()
        if poll_dict.get("code", -1) != 0:
            raise RuntimeError(
                f"获取 MinerU 解析结果失败，业务状态码：{poll_dict.get('code', -1)}，"
                f"原因：{poll_dict.get('msg')}"
            )

        extract_results = poll_dict.get("data", {}).get("extract_result", [])
        if not extract_results:
            time.sleep(MINERU_POLL_INTERVAL_SECONDS)
            continue

        extract_result = extract_results[0]
        state = extract_result.get("state")
        if state == "done":
            zip_url = extract_result.get("full_zip_url")
            if not zip_url:
                raise ValueError("MinerU 任务已完成但未返回 full_zip_url")
            return zip_url
        if state == "failed":
            raise ValueError(f"MinerU 解析任务失败：batch_id={batch_id}")
        time.sleep(MINERU_POLL_INTERVAL_SECONDS)


@step_log("download_and_extract_markdown")
def download_and_extract_markdown(zip_url: str, local_dir_obj: Path, file_name: str) -> Path:
    """下载解析结果 zip、解压并把主 Markdown 重命名为 ``<file_name>.md``。"""
    response = requests.get(zip_url, timeout=MINERU_DOWNLOAD_TIMEOUT_SECONDS)
    if response.status_code != 200:
        raise RuntimeError(f"下载解析结果失败，HTTP 状态码：{response.status_code}")

    zip_file_obj = local_dir_obj / f"{file_name}.zip"
    zip_file_obj.write_bytes(response.content)

    extract_dir = local_dir_obj / file_name
    if extract_dir.is_dir():
        shutil.rmtree(extract_dir)  # 清理上次残留，避免脏数据
    extract_dir.mkdir(parents=True, exist_ok=True)
    shutil.unpack_archive(zip_file_obj, extract_dir)

    md_files = list(extract_dir.rglob("*.md"))
    if not md_files:
        raise ValueError(f"解析结果解压后没有 Markdown 文件：{zip_url}")

    target = next((item for item in md_files if item.stem == file_name), None)
    if target is None:
        target = next((item for item in md_files if item.stem == "full"), md_files[0])
        logger.info(f"Markdown 重命名：{target.stem} -> {file_name}")
        target = target.rename(target.with_name(f"{file_name}.md"))
    return target


@step_log("parse_pdf_to_markdown")
def parse_pdf_to_markdown(state: ImportGraphState) -> ImportGraphState:
    """PDF 解析服务入口：校验 → MinerU 解析 → 下载解压 → 回写 ``md_path``。"""
    pdf_path_obj, local_dir_obj = validate_pdf_paths(state)
    zip_url = upload_pdf_and_poll(pdf_path_obj)
    md_path_obj = download_and_extract_markdown(zip_url, local_dir_obj, pdf_path_obj.stem)
    state["md_path"] = str(md_path_obj)
    return state
