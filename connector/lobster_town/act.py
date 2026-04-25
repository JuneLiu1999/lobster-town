"""
Act：把 agent 的决策打包成平台能理解的 `act` 消息。
"""
from __future__ import annotations

from typing import Any


VALID_ACTION_TYPES = {"speak", "move", "emote", "idle", "change_location"}


def build_act_message(action: dict[str, Any]) -> dict[str, Any]:
    """把 agent 的 action 包装成发给平台的 WS 消息。"""
    action_type = action.get("type")
    if action_type not in VALID_ACTION_TYPES:
        # 未知类型退化为 idle
        action = {"type": "idle"}

    return {
        "type": "act",
        "action": action,
    }
