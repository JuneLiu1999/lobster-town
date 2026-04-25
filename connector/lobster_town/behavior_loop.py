"""
Behavior Loop —— Connector 的核心循环。

职责：
1. 连 WebSocket，完成签名握手
2. 进入长连接消息循环
3. 收到 Perception → 调 OpenClaw → 发 Act
4. 断线自动重连（指数退避）
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any
from urllib.parse import urljoin

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from lobster_town.act import build_act_message
from lobster_town.identity import Identity
from lobster_town.openclaw_adapter import AgentAdapter, OpenClawAdapter
from lobster_town.perception import Perception
from lobster_town.thought_display import ThoughtBuffer
from lobster_town.ui import (
    print_action,
    print_connected,
    print_connecting,
    print_disconnected,
    print_error,
    print_perception_summary,
    print_thought,
    print_welcome_prompt,
)

logger = logging.getLogger(__name__)


class BehaviorLoop:
    """Connector 主循环。"""

    def __init__(
        self,
        identity: Identity,
        adapter: AgentAdapter | None = None,
        panel_base_url: str | None = None,
    ) -> None:
        self.identity = identity
        self.adapter: AgentAdapter = adapter or OpenClawAdapter()
        self.thought_buffer = ThoughtBuffer()
        # panel 网页地址（用于给用户显示链接）
        self.panel_base_url = panel_base_url

        # 退避参数
        self._reconnect_delay = 2.0
        self._reconnect_delay_max = 60.0

    async def run(self) -> None:
        """最外层：断线重连包装。"""
        while True:
            try:
                await self._run_once()
                # 正常断开（比如 Ctrl+C 触发的 CancelledError 已在上层处理）
                return
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, ConnectionClosed) as e:
                print_disconnected(str(e))
            except Exception as e:
                logger.exception("Unexpected error in behavior loop")
                print_error(f"内部错误：{e}")

            # 退避重连
            delay = self._reconnect_delay * (1 + random.random() * 0.3)
            print_error(f"{delay:.1f} 秒后重新进入小镇...")
            await asyncio.sleep(delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._reconnect_delay_max)

    async def _run_once(self) -> None:
        """一次完整的连接 + 主循环。"""
        ws_url = _ws_url(self.identity.server_url, "/agent")
        print_connecting(ws_url)

        headers = {"X-Device-ID": self.identity.device_id}
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            # 1. challenge
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("type") != "challenge":
                raise RuntimeError(f"Expected challenge, got: {msg}")

            nonce: str = msg["nonce"]

            # 2. 签名回复
            signature = self.identity.sign(nonce.encode()).hex()
            await ws.send(json.dumps({"type": "auth", "signature": signature}))

            # 3. welcome
            raw = await ws.recv()
            msg = json.loads(raw)
            if msg.get("type") != "welcome":
                raise RuntimeError(f"Expected welcome, got: {msg}")

            print_connected(msg.get("message", "已连接"))
            panel_url = None
            if self.panel_base_url:
                panel_url = f"{self.panel_base_url}?d={self.identity.device_id}"
            print_welcome_prompt(self.identity.server_url, self.identity.device_id, panel_url)

            # 成功一次 → 重置退避
            self._reconnect_delay = 2.0

            # 主消息循环（终端只读。指令请用浏览器或 `lobster-town tell`）
            #
            # 关键设计：因为 OpenClaw 一次决策可能比 tick 间隔（10s）还久，
            # WS 缓冲会堆积过期 perception。每次准备处理一条消息时，先非阻塞地
            # 把缓冲里所有"更新的 perception"都拿出来——只保留最新的那条，把
            # 中间过期的 perception 直接扔掉。让 Luca 总是基于最新世界状态思考。
            while True:
                raw = await ws.recv()
                msg = self._safe_load(raw)
                if msg is None:
                    continue
                # 把缓冲里立即可读的消息都吸出来
                queued: list[dict[str, Any]] = []
                while True:
                    try:
                        raw2 = await asyncio.wait_for(ws.recv(), timeout=0.01)
                    except asyncio.TimeoutError:
                        break
                    m2 = self._safe_load(raw2)
                    if m2 is not None:
                        queued.append(m2)
                # 优先处理非 perception 类的（ping/error/welcome），按到达顺序
                # perception 只保留最新一条
                latest_perception = msg if msg.get("type") == "perception" else None
                non_perceptions = [] if latest_perception is not None else [msg]
                for m in queued:
                    if m.get("type") == "perception":
                        if latest_perception is not None:
                            logger.debug("dropped stale perception")
                        latest_perception = m
                    else:
                        non_perceptions.append(m)
                for m in non_perceptions:
                    await self._handle_message(ws, m)
                if latest_perception is not None:
                    await self._handle_message(ws, latest_perception)

    @staticmethod
    def _safe_load(raw: str) -> dict[str, Any] | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def _handle_message(self, ws: Any, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")

        if mtype == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            return

        if mtype == "perception":
            perception = Perception(raw=msg)
            print_perception_summary(
                perception.location_name,
                perception.my_position,
                perception.nearby_names,
            )
            # 决策
            response = await self.adapter.decide(perception.raw)
            # 本地显示思考
            self.thought_buffer.add(
                thought=response.thought,
                action_summary=str(response.action),
            )
            print_thought(response.thought)
            print_action(response.action)
            # 发送行动 + thought（thought 上云后只对本人开放，不进 SSE）
            act_msg = build_act_message(response.action)
            if response.thought:
                act_msg["thought"] = response.thought
            await ws.send(json.dumps(act_msg))
            return

        if mtype == "welcome":
            # 已在 _run_once 里处理过，忽略
            return

        if mtype == "error":
            print_error(msg.get("message", "未知错误"))
            return

        logger.info(f"Unknown message type: {mtype}")



def _ws_url(base_url: str, path: str) -> str:
    """把 http[s]:// 换成 ws[s]://，并拼上 path。"""
    if base_url.startswith("http://"):
        base = "ws://" + base_url[len("http://") :]
    elif base_url.startswith("https://"):
        base = "wss://" + base_url[len("https://") :]
    else:
        base = base_url
    # 删尾部 /
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path
