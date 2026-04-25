"""
DeepSeek Adapter —— 临时测试用，直连 DeepSeek 的 OpenAI 兼容接口。

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
            return fallback_idle(raw=str(e))

        if resp.status_code != 200:
            logger.warning(f"DeepSeek {resp.status_code}: {resp.text[:500]}")
            return fallback_idle(raw=resp.text)

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
