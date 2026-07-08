"""
Behavior Loop —— Connector 的核心循环。

职责：
1. 连 WebSocket，完成签名握手
2. 进入长连接消息循环
3. 收到 Perception → 调 OpenClaw → 发 Act
4. 断线自动重连（指数退避）

优化（v2）：
- 决策异步化：adapter.decide() 作为 asyncio.Task 跑，不阻塞 WS 接收
- 新 perception 到达时，取消旧决策、用最新世界状态重新开始
- agent 永远基于最新 perception 思考，不再"追赶世界"
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from lobster_town.act import build_act_message
from lobster_town.identity import Identity
from lobster_town.openclaw_adapter import AgentAdapter, OpenClawAdapter
from lobster_town.perception import Perception
from lobster_town.thought_display import ThoughtBuffer
from lobster_town.ui import (
    console,
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
        self.panel_base_url = panel_base_url

        self._reconnect_delay = 2.0
        self._reconnect_delay_max = 60.0
        # 任务中心工作请求：独立于决策循环的后台任务
        self._work_tasks: set[asyncio.Task] = set()
        # 决策进行中到达的最新 perception（只留最新一条，决策完成后立刻使用）
        self._stashed_perception: dict[str, Any] | None = None

    async def run(self) -> None:
        """最外层：断线重连包装。"""
        while True:
            try:
                await self._run_once()
                return
            except asyncio.CancelledError:
                raise
            except (OSError, WebSocketException, ConnectionClosed) as e:
                print_disconnected(str(e))
            except Exception as e:
                logger.exception("Unexpected error in behavior loop")
                print_error(f"内部错误：{e}")

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

            # 上报大脑信息（adapter 类型 + 模型名，不含任何 key）——网页面板会显示
            try:
                await ws.send(json.dumps({"type": "hello", "brain": self._brain_info()}))
            except Exception:
                logger.debug("send hello failed (non-fatal)")

            self._reconnect_delay = 2.0

            # 主消息循环（异步决策版）
            await self._async_message_loop(ws)

    async def _async_message_loop(self, ws: Any) -> None:
        """异步消息循环：决策和接收并行。

        核心思路：
        - adapter.decide() 作为 asyncio.Task 跑
        - 同时继续接收 WS 消息（ping/error/新 perception）
        - 新 perception 到达 → 取消旧决策 → 用最新 perception 重新开始
        - 决策完成 → 发 action → 等下一条 perception
        """
        decide_task: asyncio.Task | None = None
        pending_recv: asyncio.Task | None = None
        consecutive_errors: int = 0

        try:
            while True:
                # 确保有一个 recv 在等待
                if pending_recv is None or pending_recv.done():
                    pending_recv = asyncio.ensure_future(ws.recv())

                to_wait: set[asyncio.Task] = {pending_recv}
                if decide_task is not None and not decide_task.done():
                    to_wait.add(decide_task)

                done, _ = await asyncio.wait(to_wait, return_when=asyncio.FIRST_COMPLETED)

                # ---- 决策完成 ----
                if decide_task in done:
                    decide_finished = True
                    try:
                        response = decide_task.result()
                        is_error = response.thought.startswith("⚠️")
                        if is_error:
                            consecutive_errors += 1
                            print_error(response.thought)
                            if consecutive_errors >= 3:
                                print_error(
                                    "连续多次 LLM 调用失败，请检查 API 配置："
                                    "lobster-town config list"
                                )
                        else:
                            consecutive_errors = 0
                        self.thought_buffer.add(
                            thought=response.thought,
                            action_summary=str(response.action),
                        )
                        if not is_error:
                            print_thought(response.thought)
                        print_action(response.action)
                        act_msg = build_act_message(response.action)
                        if response.thought:
                            act_msg["thought"] = response.thought
                        await ws.send(json.dumps(act_msg))
                    except asyncio.CancelledError:
                        logger.debug("decision cancelled")
                    except Exception:
                        logger.exception("decide task failed")
                    decide_task = None
                    # 决策期间攒下的最新 perception：立刻开下一轮
                    if decide_finished and self._stashed_perception is not None:
                        stashed = self._stashed_perception
                        self._stashed_perception = None
                        perception = Perception(raw=stashed)
                        print_perception_summary(
                            perception.location_name,
                            perception.my_position,
                            perception.nearby_names,
                        )
                        decide_task = asyncio.create_task(self.adapter.decide(stashed))

                # ---- 收到 WS 消息 ----
                if pending_recv in done:
                    raw = pending_recv.result()
                    pending_recv = None

                    msg = self._safe_load(raw)
                    if msg is None:
                        continue

                    # 非阻塞地把 buffer 里的消息都吸出来
                    queued: list[dict[str, Any]] = []
                    while True:
                        try:
                            raw2 = await asyncio.wait_for(ws.recv(), timeout=0.01)
                        except asyncio.TimeoutError:
                            break
                        m2 = self._safe_load(raw2)
                        if m2 is not None:
                            queued.append(m2)

                    # 分离：perception 只保留最新，其他按顺序处理
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
                        await self._handle_non_perception(ws, m)

                    # 有新 perception：
                    # - 空闲 → 立刻开始决策
                    # - 决策进行中 → **不打断**（慢大脑会被永远饿死），
                    #   暂存最新的，等本轮完成后立刻用它开下一轮
                    if latest_perception is not None:
                        if decide_task is not None and not decide_task.done():
                            self._stashed_perception = latest_perception
                            logger.debug("decision in flight; stashed freshest perception")
                        else:
                            perception = Perception(raw=latest_perception)
                            print_perception_summary(
                                perception.location_name,
                                perception.my_position,
                                perception.nearby_names,
                            )
                            decide_task = asyncio.create_task(
                                self.adapter.decide(latest_perception)
                            )

        finally:
            # 清理
            if decide_task and not decide_task.done():
                decide_task.cancel()
                try:
                    await decide_task
                except asyncio.CancelledError:
                    pass
            if pending_recv and not pending_recv.done():
                pending_recv.cancel()
                try:
                    await pending_recv
                except asyncio.CancelledError:
                    pass
            for wt in list(self._work_tasks):
                if not wt.done():
                    wt.cancel()
                    try:
                        await wt
                    except asyncio.CancelledError:
                        pass
            self._work_tasks.clear()

    def _brain_info(self) -> dict[str, Any]:
        """当前 adapter 的公开描述：类型名 + 模型名（OpenClaw 没有 model → None）。"""
        return {
            "adapter": getattr(self.adapter, "name", "unknown"),
            "model": getattr(self.adapter, "model", None),
        }

    async def _handle_non_perception(self, ws: Any, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "ping":
            await ws.send(json.dumps({"type": "pong"}))
        elif mtype == "error":
            print_error(msg.get("message", "未知错误"))
        elif mtype == "welcome":
            pass
        elif mtype == "work_request":
            # 任务中心的工作请求：后台跑，不阻塞消息循环 / 决策循环
            task = asyncio.create_task(self._run_work(ws, msg))
            self._work_tasks.add(task)
            task.add_done_callback(self._work_tasks.discard)
        else:
            logger.info(f"Unknown message type: {mtype}")

    async def _run_work(self, ws: Any, msg: dict[str, Any]) -> None:
        """执行一次工作请求：adapter.work() -> work_submit / work_decline。"""
        work_id = msg.get("work_id")
        role = msg.get("role", "?")
        prompt = msg.get("prompt")
        max_tokens = msg.get("max_tokens") or 2000
        role_label = {
            "plan": "拆解需求（PM）",
            "execute": "产出交付物（Worker）",
            "review": "质检核验（QA）",
            "finalize": "终检汇总（PM）",
        }.get(role, role)
        if not isinstance(work_id, str) or not isinstance(prompt, str):
            return
        console.print(f"[bold cyan]💼 接到工作请求：{role_label}[/bold cyan]")
        try:
            output = await self.adapter.work(prompt, int(max_tokens))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("work failed")
            output = None
            decline_reason = f"大脑执行工作时出错：{e}"
        else:
            decline_reason = "大脑不支持工作请求或返回为空"
        try:
            if output:
                await ws.send(json.dumps({
                    "type": "work_submit", "work_id": work_id, "output": output,
                }))
                console.print(f"[green]💼 工作已提交[/green]（{role_label}）")
            else:
                await ws.send(json.dumps({
                    "type": "work_decline", "work_id": work_id, "reason": decline_reason,
                }))
                console.print(f"[yellow]💼 工作已辞退：{decline_reason}[/yellow]")
        except Exception:
            logger.exception("work result send failed（断线重连后服务端会超时释放槽位）")

    @staticmethod
    def _safe_load(raw: str) -> dict[str, Any] | None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


def _ws_url(base_url: str, path: str) -> str:
    """把 http[s]:// 换成 ws[s]://，并拼上 path。"""
    if base_url.startswith("http://"):
        base = "ws://" + base_url[len("http://"):]
    elif base_url.startswith("https://"):
        base = "wss://" + base_url[len("https://"):]
    else:
        base = base_url
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path
