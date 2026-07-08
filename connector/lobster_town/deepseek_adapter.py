"""
DeepSeek Adapter —— **已废弃**，仅为老用户兼容保留。

新用户请用 `lobster-town setup` 走 direct 模式（generic_llm_adapter），
功能是超集：持久连接池、prompt cache、任意 OpenAI 兼容端点。

读取 env 里的 `LOBSTER_DEEPSEEK_KEY`（不落盘、不打日志）。
模型默认 `deepseek-chat`，URL `https://api.deepseek.com/v1/chat/completions`。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx

from lobster_town.openclaw_adapter import (
    AgentAdapter,
    AgentResponse,
    SKILL_INLINE,  # 保留以兼容老脚本
    build_full_skill_prompt,
    build_perception_summary,
    fallback_idle,
    find_json_blob,
)

logger = logging.getLogger(__name__)


def _deepseek_error_hint(status: int) -> str:
    """根据 HTTP 状态码返回面向用户的中文提示。"""
    if status == 401:
        return "⚠️ DeepSeek API 认证失败（401）：API Key 无效或已过期，请更换 Key。"
    if status == 402:
        return "⚠️ DeepSeek API 余额不足（402）：账户余额已用完，请前往 platform.deepseek.com 充值。"
    if status == 429:
        return "⚠️ DeepSeek API 请求过于频繁（429）：已触发速率限制，稍后会自动重试。"
    if 500 <= status < 600:
        return f"⚠️ DeepSeek 服务端错误（{status}）：服务暂时不可用，稍后会自动重试。"
    return f"⚠️ DeepSeek API 调用失败（HTTP {status}）：请检查配置或联系 DeepSeek。"


class DeepSeekAdapter(AgentAdapter):
    """直连 DeepSeek chat completions。"""

    name = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com/v1",
        timeout_seconds: int = 30,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def is_available(self) -> bool:
        return bool(self._api_key)

    async def decide(self, perception: dict[str, Any]) -> AgentResponse:
        summary = build_perception_summary(perception)
        user_msg = (
            "【龙虾小镇 · 场景感知】\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)
            + "\n\n严格按系统提示里的规则，只返回一个 JSON 对象（thought + action），不要任何解释或 markdown 包裹。"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": build_full_skill_prompt(perception)},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.8,
            "max_tokens": 400,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as http:
                resp = await http.post(
                    self.base_url + "/chat/completions",
                    json=payload,
                    headers=headers,
                )
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            logger.warning(f"DeepSeek call failed: {e!r}")
            if isinstance(e, asyncio.TimeoutError):
                hint = "⚠️ DeepSeek API 请求超时：网络连接缓慢或服务端无响应，稍后会自动重试。"
            else:
                hint = f"⚠️ DeepSeek API 网络错误：无法连接到 {self.base_url}，请检查网络。"
            return fallback_idle(raw=str(e), user_hint=hint)

        if resp.status_code != 200:
            logger.warning(f"DeepSeek {resp.status_code}: {resp.text[:500]}")
            hint = _deepseek_error_hint(resp.status_code)
            return fallback_idle(raw=resp.text, user_hint=hint)

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        tokens = usage.get("total_tokens")

        blob = find_json_blob(text) or text
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            logger.warning("DeepSeek reply not JSON; falling back to idle")
            return fallback_idle(raw=text)

        thought = str(parsed.get("thought", ""))
        action = parsed.get("action")
        if not isinstance(action, dict) or "type" not in action:
            return fallback_idle(raw=text)

        return AgentResponse(thought=thought, action=action, raw_text=text, tokens_used=tokens)


def from_env() -> DeepSeekAdapter | None:
    """若环境里配了 key 就返回 adapter，否则 None。不落盘、不打印 key。"""
    key = os.environ.get("LOBSTER_DEEPSEEK_KEY")
    if not key:
        return None
    return DeepSeekAdapter(api_key=key)
