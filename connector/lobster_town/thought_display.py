"""
思考过程：只在本地保存和显示，不上云。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ThoughtRecord:
    timestamp: datetime
    thought: str
    action_summary: str


@dataclass
class ThoughtBuffer:
    """本地的思考历史缓冲。"""

    max_size: int = 50
    records: deque[ThoughtRecord] = field(default_factory=lambda: deque(maxlen=50))

    def add(self, thought: str, action_summary: str) -> None:
        self.records.append(
            ThoughtRecord(
                timestamp=datetime.now(),
                thought=thought,
                action_summary=action_summary,
            )
        )

    def latest(self) -> ThoughtRecord | None:
        return self.records[-1] if self.records else None
