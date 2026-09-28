"""对话 / 视觉语言模型客户端（OpenAI 兼容协议，进程内按「模型+JSON模式」缓存）。"""
from __future__ import annotations

from langchain_core.exceptions import LangChainException
from langchain_openai import ChatOpenAI

from app.shared.config import settings
from app.shared.runtime.logger import logger

_DEFAULT_LLM_MODEL = "qwen3-32b"
_DEFAULT_TEMPERATURE = 0.1
_llm_client_cache: dict[tuple[str, bool], ChatOpenAI] = {}


def get_llm_client(model: str | None = None, json_mode: bool = False) -> ChatOpenAI:
    """获取 ChatOpenAI 客户端（带进程内缓存）。

    :param model: 模型名，优先级：入参 > ``settings.llm.llm_model`` > 内置默认值
    :param json_mode: 是否开启 JSON 输出模式（``response_format=json_object``）
    """
    target_model = model or settings.llm.llm_model or _DEFAULT_LLM_MODEL
    cache_key = (target_model, json_mode)
    if cache_key in _llm_client_cache:
        return _llm_client_cache[cache_key]

    if not settings.llm.api_key:
        raise ValueError("配置缺失：请在 .env 中配置 OPENAI_API_KEY（大模型 API 密钥）")
    if not settings.llm.base_url:
        raise ValueError("配置缺失：请在 .env 中配置 OPENAI_BASE_URL（API 接口基础地址）")

    # extra_body：千问等国产模型专属参数（LangChain 透传至 API），关闭思考链输出
    extra_body = {"enable_thinking": False}
    model_kwargs: dict = {}
    if json_mode:
        model_kwargs["response_format"] = {"type": "json_object"}

    try:
        client = ChatOpenAI(
            model=target_model,
            temperature=settings.llm.temperature or _DEFAULT_TEMPERATURE,
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
            extra_body=extra_body,
            model_kwargs=model_kwargs,
        )
    except LangChainException as exc:
        raise RuntimeError(f"模型【{target_model}】初始化失败（LangChain 层）：{exc}") from exc

    _llm_client_cache[cache_key] = client
    logger.info(f"LLM 客户端已初始化并缓存：model={target_model}, json_mode={json_mode}")
    return client
