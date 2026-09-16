from __future__ import annotations

from typing import Any

from reviewer.config import DEFAULT_API_KEY, DEFAULT_BASE_URL


class LLMError(Exception):
    pass


def get_chat_model(model: str) -> Any:
    # Imported lazily so the rest of the package stays importable (and unit
    # testable) without a provider SDK installed.
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {"model": model}
    if DEFAULT_BASE_URL:
        kwargs["base_url"] = DEFAULT_BASE_URL
    if DEFAULT_API_KEY:
        kwargs["api_key"] = DEFAULT_API_KEY
    return ChatOpenAI(**kwargs)
