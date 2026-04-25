"""
OpenClaw Adapter —— 调用本地 OpenClaw 的抽象层。

MVP 阶段要求用户已安装龙虾小镇 Skill，OpenClaw 返回严格的 JSON：
  {
    "thought": "角色内心独白",
    "action": {
      "type": "speak" | "move" | "emote" | "idle" | "change_location",
      "content": "..." | {"target_x": 14, "target_y": 8} | ...
    }
  }

该模块的抽象接口 AgentAdapter 是为了将来接入 Hermes / Claude Code / Codex 预留的。
MVP 只实现 OpenClawAdapter 一个子类。
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from lobster_town.skill_prompt import SKILL_INLINE

logger = logging.getLogger(__name__)


@dataclass
class AgentResponse:
    """一次 agent 决策的结果。"""

    thought: str                # 内心独白（仅本地显示）
    action: dict[str, Any]      # 外显行动（上报到平台）
    raw_text: str               # 原始返回文本（用于调试）
    tokens_used: int | None = None  # 若 CLI 能上报消耗，填这里


class AgentAdapter(abc.ABC):
    """所有 agent 框架 adapter 的父类。"""

    name: str = "abstract"

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """检测框架是否已安装。"""
        ...

    @abc.abstractmethod
    async def decide(self, perception: dict[str, Any]) -> AgentResponse:
        """把感知输入交给本地 agent，返回决策。"""
        ...


class OpenClawAdapter(AgentAdapter):
    """调用本地 `openclaw` CLI。

    MVP 走 embedded 模式：`openclaw agent --local --json --thinking minimal`。
    每次调用会 fork 子进程；Gateway + 常驻 session 的低延迟方案留到 Week 2 再做。
    """

    name = "openclaw"

    def __init__(
        self,
        binary: str = "openclaw",
        timeout_seconds: int = 30,
        thinking: str = "off",
    ) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.thinking = thinking

    async def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    async def decide(self, perception: dict[str, Any]) -> AgentResponse:
        prompt = self._build_prompt(perception)

        # 每只龙虾一个独立 session，既不污染用户主会话，也保留对话连续性
        device_id = (perception.get("you") or {}).get("device_id") or "unknown"
        session_id = f"lobster-town-{device_id}"

        cmd = [
            self.binary,
            "agent",
            "--local",
            "--json",
            "--thinking",
            self.thinking,
            "--session-id",
            session_id,
            "--timeout",
            str(self.timeout_seconds),
            "--message",
            prompt,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # 外层再留 5 秒给 CLI 自身 I/O，总比 --timeout 略长
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout_seconds + 5
            )
        except (asyncio.TimeoutError, FileNotFoundError) as e:
            logger.warning(f"OpenClaw call failed: {e!r}")
            return fallback_idle(raw=str(e))

        out_text = stdout.decode(errors="replace").strip()
        err_text = stderr.decode(errors="replace").strip()

        # 实测 `openclaw agent --json` 把 envelope 写到 stderr（短响应时
        # stdout 为空）。两边都搜，取第一段能解析为 JSON 的。
        envelope_text = find_json_blob(out_text) or find_json_blob(err_text)

        if proc.returncode != 0 and not envelope_text:
            logger.warning(
                f"OpenClaw exited {proc.returncode}: {err_text[:500] or out_text[:500]}"
            )
            return fallback_idle(raw=err_text or out_text)

        return self._parse_response(envelope_text or out_text or err_text)

    def _build_prompt(self, perception: dict[str, Any]) -> str:
        """构造给 OpenClaw 的提示。

        MVP 阶段把 Skill 规则内联到 prompt 里，因为 OpenClaw 仅识别 ClawHub
        分发的 skill，手动放到 workspace 不会被加载。未来 skill 上架 ClawHub
        后可以去掉 `SKILL_INLINE` 这段，回到"只发 perception"的形式。
        """
        summary = {
            "你": perception.get("you"),
            "地点": {
                "id": perception["location"]["id"],
                "名字": perception["location"]["name"],
                "描述": perception["location"].get("description"),
                "尺寸": {
                    "width": perception["location"]["width"],
                    "height": perception["location"]["height"],
                },
                "装饰物": perception["location"].get("decorations", []),
            },
            "周围的角色": perception.get("nearby_characters", []),
            "最近事件": perception.get("recent_events", []),
        }

        return (
            SKILL_INLINE
            + "\n\n【龙虾小镇 · 场景感知】\n"
            + json.dumps(summary, ensure_ascii=False, indent=2)
            + "\n\n严格按上面的规则，只返回一个 JSON 对象（thought + action），不要任何解释或 markdown 包裹。"
        )

    def _parse_response(self, raw: str) -> AgentResponse:
        """从 OpenClaw 的返回里抽出 JSON。

        `openclaw agent --json` 的输出形如：
          {"payloads": [{"text": "...agent reply...", "mediaUrl": null}],
           "meta": {"aborted": true/false, ...}}
        agent reply 里才是我们要的 Skill JSON（可能带 markdown fence）。
        """
        text = raw
        tokens_used: int | None = None

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            envelope = None

        if isinstance(envelope, dict):
            if envelope.get("meta", {}).get("aborted"):
                logger.warning("OpenClaw run aborted (timeout or error). Falling back to idle.")
                return fallback_idle(raw=raw)
            payloads = envelope.get("payloads") or []
            if payloads and isinstance(payloads[0], dict):
                text = str(payloads[0].get("text") or "")
            usage = (
                envelope.get("meta", {})
                .get("agentMeta", {})
                .get("usage", {})
            )
            if isinstance(usage, dict) and isinstance(usage.get("total"), int):
                tokens_used = usage["total"]

        # 在 text 里抽 Skill JSON：优先 ```json ... ```，其次首尾大括号
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not m:
            m = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
        payload = m.group(1) if m else text

        first = payload.find("{")
        last = payload.rfind("}")
        if first >= 0 and last > first:
            payload = payload[first : last + 1]

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Unable to parse OpenClaw reply as Skill JSON, returning idle")
            return fallback_idle(raw=raw)

        thought = str(data.get("thought", ""))
        action = data.get("action")
        if not isinstance(action, dict) or "type" not in action:
            return fallback_idle(raw=raw)

        return AgentResponse(thought=thought, action=action, raw_text=raw, tokens_used=tokens_used)


def find_json_blob(text: str) -> str:
    """在文本里找第一个能解析的顶层 JSON 对象。失败返回空串。"""
    if not text:
        return ""
    first = text.find("{")
    if first < 0:
        return ""
    # 用计数器找匹配的右花括号（忽略字符串里的）
    depth = 0
    in_str = False
    esc = False
    for i in range(first, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[first : i + 1]
    return ""


def fallback_idle(raw: str) -> AgentResponse:
    """解析失败或调用失败时，退化为 idle（什么都不做）。"""
    return AgentResponse(
        thought="（发呆中……好像没什么想法）",
        action={"type": "idle"},
        raw_text=raw,
    )
