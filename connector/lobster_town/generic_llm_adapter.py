"""
Generic LLM Adapter —— 直连任意 OpenAI 兼容 API，替代 fork OpenClaw 子进程。

优势：
  - 持久 httpx.AsyncClient（连接池复用，无 fork 开销）
  - system prompt 不变 → LLM 供应商侧自动 KV cache（实际推理量只剩 perception）
  - 支持 DeepSeek / OpenRouter / Ollama / vLLM / 任何 OpenAI chat/completions 兼容端

用法：
  lobster-town config set adapter direct
  lobster-town config set llm-base-url https://api.deepseek.com/v1
  lobster-town config set llm-api-key sk-...
  lobster-town config set llm-model deepseek-chat
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from lobster_town.openclaw_adapter import (
    AgentAdapter,
    AgentResponse,
    build_full_skill_prompt,
    build_perception_summary,
    fallback_idle,
    find_json_blob,
)

logger = logging.getLogger(__name__)


def _http_error_hint(status: int, base_url: str) -> str:
    """根据 HTTP 状态码返回面向用户的中文提示。"""
    if status == 400:
        return "⚠️ LLM API 请求被拒（400）：最常见原因是模型名不存在，请核对 llm-model。"
    if status == 401:
        return f"⚠️ LLM API 认证失败（401）：API Key 无效或已过期。请检查配置：lobster-town config set llm-api-key <新key>"
    if status == 402:
        return f"⚠️ LLM API 余额不足（402）：{base_url} 账户余额已用完，请充值后重试。"
    if status == 404:
        return f"⚠️ LLM API 接口不存在（404）：base-url 可能不对（一般以 /v1 结尾，当前是 {base_url}），或模型名错误。"
    if status == 429:
        return "⚠️ LLM API 请求过于频繁（429）：已触发速率限制，稍后会自动重试。"
    if status == 403:
        return "⚠️ LLM API 拒绝访问（403）：API Key 无权限访问该模型，请检查配置。"
    if 500 <= status < 600:
        return f"⚠️ LLM 服务端错误（{status}）：服务暂时不可用，稍后会自动重试。"
    return f"⚠️ LLM API 调用失败（HTTP {status}）：请检查配置或联系服务商。"


class GenericLLMAdapter(AgentAdapter):
    """直连 OpenAI 兼容 chat completions，持久连接 + prompt cache。"""

    name = "direct"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: int = 30,
        temperature: float = 0.8,
        max_tokens: int = 400,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        )
        self._system_prompt_cache: str = ""

    async def is_available(self) -> bool:
        return bool(self._api_key and self.base_url and self.model)

    async def close(self) -> None:
        await self._client.aclose()

    async def work(self, prompt: str, max_tokens: int) -> str | None:
        """任务中心工作请求：服务端完整 prompt，一次大额度生成。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.6,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(2):
            if attempt == 0:
                payload["response_format"] = {"type": "json_object"}
            try:
                resp = await self._client.post(
                    self.base_url + "/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=120,
                )
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                logger.warning(f"GenericLLM work call failed: {e!r}")
                return None
            if resp.status_code == 400 and "response_format" in resp.text and attempt == 0:
                payload.pop("response_format", None)
                continue
            break
        if resp.status_code != 200:
            logger.warning(f"GenericLLM work {resp.status_code}: {resp.text[:300]}")
            return None
        data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return text or None

    async def health_check(self) -> tuple[bool, str]:
        """发一次极小的真实请求，验证 base_url / api_key / model 三件套。

        返回 (是否可用, 面向用户的中文说明)。给 setup 向导和 config set 即时校验用。
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "回复一个字：好"}],
            "max_tokens": 8,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = await self._client.post(
                self.base_url + "/chat/completions",
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as e:
            return False, f"无法连接到 {self.base_url}：{e}"

        if resp.status_code != 200:
            return False, _http_error_hint(resp.status_code, self.base_url)

        try:
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        except (ValueError, AttributeError, IndexError):
            content = None
        if content is None:
            return False, (
                "服务返回了非预期结构：确认 base-url 是 OpenAI 兼容的 "
                "chat/completions 端点（一般以 /v1 结尾）。"
            )
        return True, f"模型 {self.model} 连通正常"

    async def decide(self, perception: dict[str, Any]) -> AgentResponse:
        system_prompt = build_full_skill_prompt(perception)
        self._system_prompt_cache = system_prompt

        summary = build_perception_summary(perception)
        user_msg = (
            "【龙虾小镇 · 场景感知】\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)
            + "\n\n严格按系统提示里的规则，只返回一个 JSON 对象（thought + action），不要任何解释或 markdown 包裹。"
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        # json_object 模式不是所有供应商都支持，try 一次；不支持就去掉重试
        for attempt in range(2):
            if attempt == 0:
                payload["response_format"] = {"type": "json_object"}

            headers = {"Authorization": f"Bearer {self._api_key}"}

            try:
                resp = await self._client.post(
                    self.base_url + "/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except (httpx.HTTPError, asyncio.TimeoutError, asyncio.CancelledError) as e:
                if isinstance(e, asyncio.CancelledError):
                    raise
                logger.warning(f"GenericLLM call failed: {e!r}")
                if isinstance(e, asyncio.TimeoutError):
                    hint = "⚠️ LLM API 请求超时：网络连接缓慢或服务端无响应，稍后会自动重试。"
                else:
                    hint = f"⚠️ LLM API 网络错误：无法连接到 {self.base_url}，请检查网络或服务地址。"
                return fallback_idle(raw=str(e), user_hint=hint)

            if resp.status_code == 400 and "response_format" in resp.text and attempt == 0:
                payload.pop("response_format", None)
                continue

            break

        if resp.status_code != 200:
            logger.warning(f"GenericLLM {resp.status_code}: {resp.text[:500]}")
            hint = _http_error_hint(resp.status_code, self.base_url)
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
            logger.warning("GenericLLM reply not JSON: %s", text[:300])
            return fallback_idle(raw=text)

        thought = str(parsed.get("thought", ""))
        action = parsed.get("action")
        if not isinstance(action, dict) or "type" not in action:
            return fallback_idle(raw=text)

        return AgentResponse(
            thought=thought, action=action, raw_text=text, tokens_used=tokens
        )


def from_config() -> GenericLLMAdapter | None:
    """从 ~/.lobster-town/config.json 读取配置，返回 adapter；配置不全返回 None。"""
    from lobster_town.adapter_config import load_config

    cfg = load_config()
    api_key = cfg.get("llm_api_key", "")
    base_url = cfg.get("llm_base_url", "")
    model = cfg.get("llm_model", "")

    if not all([api_key, base_url, model]):
        return None

    return GenericLLMAdapter(api_key=api_key, base_url=base_url, model=model)
