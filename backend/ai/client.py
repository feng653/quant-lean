"""DeepSeek AI 客户端 —— 封装 DeepSeek Chat Completion API 调用."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from backend.config import settings

logger = logging.getLogger("quant_platform.ai")


@dataclass(frozen=True)
class ChatResult:
    """Successful model response with provider-reported token usage."""

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class DeepSeekClient:
    """DeepSeek API 异步客户端。

    用法:
        client = DeepSeekClient()
        result = await client.chat(
            BACKTEST_ANALYSIS_PROMPT,
            strategy_name="均线交叉",
            experiment_id=42,
            ...
        )
    """

    # DeepSeek Chat Completion endpoint
    CHAT_ENDPOINT = "/v1/chat/completions"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key or settings.DEEPSEEK_API_KEY
        self._base_url = (base_url or settings.DEEPSEEK_BASE_URL).rstrip("/")
        self._model = model or self.DEFAULT_MODEL
        self._timeout = timeout

        if not self._api_key:
            logger.warning("DEEPSEEK_API_KEY is empty — AI calls will fail")

    @property
    def _client(self) -> httpx.AsyncClient:
        """获取或创建 httpx 异步客户端（每次调用重建，避免连接复用问题）。"""
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

    async def chat(
        self,
        prompt_template: str,
        system_prompt: str = "你是一名专业的量化金融分析师。",
        **kwargs: Any,
    ) -> str:
        """填充 prompt 模板并返回文本（向后兼容入口）。"""
        result = await self.chat_with_usage(
            prompt_template,
            system_prompt=system_prompt,
            **kwargs,
        )
        return result.text

    async def chat_with_usage(
        self,
        prompt_template: str,
        system_prompt: str = "你是一名专业的量化金融分析师。",
        **kwargs: Any,
    ) -> ChatResult:
        """填充 prompt 模板并调用 DeepSeek Chat API。

        Args:
            prompt_template: 包含 {placeholder} 的模板字符串
            system_prompt: 系统角色提示
            **kwargs: 模板占位符的填充值

        Returns:
            AI 回答及模型、token 用量

        Raises:
            ValueError: API key 未配置
            httpx.HTTPError: 网络请求失败
            KeyError: 模板占位符未填充
        """
        if not self._api_key:
            raise ValueError("DeepSeek API key 未配置，请在 .env 中设置 DEEPSEEK_API_KEY")

        # 填充模板
        try:
            prompt = prompt_template.format(**kwargs)
        except KeyError as e:
            raise KeyError(f"Prompt 模板缺少占位符: {e}") from e

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 2000,
        }

        logger.info("Calling DeepSeek API: model=%s, prompt_len=%d", self._model, len(prompt))

        async with self._client as client:
            try:
                response = await client.post(self.CHAT_ENDPOINT, json=payload)
                response.raise_for_status()
                data = response.json()

                choice = data["choices"][0]
                content: str = choice["message"]["content"]
                usage = data.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(usage.get("completion_tokens") or 0)
                total_tokens = int(
                    usage.get("total_tokens")
                    or prompt_tokens + completion_tokens
                )

                logger.info(
                    "DeepSeek API success: tokens_used=%s",
                    total_tokens,
                )
                return ChatResult(
                    text=content.strip(),
                    model=str(data.get("model") or self._model),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )

            except httpx.HTTPStatusError as e:
                logger.error("DeepSeek API HTTP error: %s %s", e.response.status_code, e.response.text[:500])
                raise
            except httpx.RequestError as e:
                logger.error("DeepSeek API network error: %s", e)
                raise
            except (KeyError, IndexError) as e:
                logger.error("DeepSeek API unexpected response format: %s", e)
                raise ValueError(f"DeepSeek API 返回格式异常: {e}") from e


# 全局单例
_deepseek_client: DeepSeekClient | None = None


def get_deepseek_client() -> DeepSeekClient:
    """获取 DeepSeekClient 全局单例。"""
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = DeepSeekClient()
    return _deepseek_client
