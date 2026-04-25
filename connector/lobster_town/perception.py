"""
Perception 处理：收到平台的感知消息后，
1) 缓存到本地（用于 UI 展示"你看到了什么"）
2) 交给 OpenClaw 做决策
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Perception:
    """平台推送给 agent 的感知数据。"""

    raw: dict[str, Any]

    @property
    def location_id(self) -> str:
        return self.raw["location"]["id"]

    @property
    def location_name(self) -> str:
        return self.raw["location"]["name"]

    @property
    def my_position(self) -> tuple[int, int]:
        p = self.raw["you"]["position"]
        return p["x"], p["y"]

    @property
    def nearby_names(self) -> list[str]:
        return [c["name"] for c in self.raw.get("nearby_characters", [])]

    @property
    def recent_events(self) -> list[dict[str, Any]]:
        return self.raw.get("recent_events", [])
