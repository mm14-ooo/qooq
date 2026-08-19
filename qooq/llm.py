"""Anthropic client construction and shared error handling."""

from __future__ import annotations

import anthropic


class QooqError(RuntimeError):
    pass


def build_client(**kwargs) -> anthropic.Anthropic:
    """Create a client, resolving credentials the way the SDK does.

    An unset ANTHROPIC_API_KEY is not necessarily fatal — the SDK also reads
    ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles — so we let the SDK try
    and only translate the failure into something a player can act on.
    """
    try:
        return anthropic.Anthropic(**kwargs)
    except Exception as exc:  # pragma: no cover - depends on local credentials
        raise QooqError(
            "無法建立 Anthropic client：%s\n"
            "請設定 ANTHROPIC_API_KEY，或執行 `ant auth login`。" % exc
        ) from exc


def describe_api_error(exc: Exception) -> str:
    """A one-line, player-facing explanation of an API failure."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "API 金鑰無效或未設定（ANTHROPIC_API_KEY）。"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "這把金鑰沒有呼叫此模型的權限。"
    if isinstance(exc, anthropic.NotFoundError):
        return "找不到指定的模型，請檢查 QOOQ_NARRATOR_MODEL。"
    if isinstance(exc, anthropic.RateLimitError):
        retry = exc.response.headers.get("retry-after", "60")
        return f"觸發速率限制，請於 {retry} 秒後再試。"
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return f"Anthropic 服務暫時異常（{exc.status_code}），稍後重試。"
        return f"API 錯誤 {exc.status_code}：{exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "連線失敗，請檢查網路。"
    return f"未預期的錯誤：{exc}"
