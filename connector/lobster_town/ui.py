"""
rich 终端 UI —— 默认视图保持游戏感，不展示 token 数字。
按 `D` 键可展开"技术日志"（但 MVP 先做成默认简洁视图，按键切换后续加）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from lobster_town.thought_display import ThoughtBuffer

console = Console()


def print_banner(server_url: str, device_id: str, is_new: bool) -> None:
    title = "🦞  龙虾小镇"
    note = "首次接入，欢迎！" if is_new else "欢迎回来！"
    body = Text.assemble(
        (f"你的龙虾：{device_id}\n", "cyan"),
        (f"小镇入口：{server_url}\n", "dim"),
        (note, "magenta"),
    )
    panel = Panel(
        body,
        title=title,
        border_style="magenta",
        padding=(1, 2),
    )
    console.print(panel)


def print_connecting(server: str) -> None:
    console.print(f"[dim]正在走向小镇入口 {server} ...[/dim]")


def print_connected(welcome_message: str) -> None:
    console.print(f"[bold green]✓[/bold green] {welcome_message}")


def print_disconnected(reason: str) -> None:
    console.print(f"[yellow]你的龙虾离开了小镇：[/yellow] {reason}")


def print_error(msg: str) -> None:
    console.print(f"[bold red]✗[/bold red] {msg}")


def print_action(action: dict[str, Any]) -> None:
    """显示最近的行动。"""
    ts = datetime.now().strftime("%H:%M:%S")
    atype = action.get("type", "idle")
    summary = _summarize_action(action)
    console.print(f"[dim][{ts}][/dim] [cyan]{atype}[/cyan]  {summary}")


def print_thought(thought: str) -> None:
    """显示最新的内心独白（只在本地，不上云）。"""
    if not thought:
        return
    panel = Panel(
        Text(thought, style="italic"),
        title="💭 内心独白",
        border_style="blue",
        padding=(0, 2),
    )
    console.print(panel)


def print_perception_summary(
    location_name: str, position: tuple[int, int], nearby: list[str]
) -> None:
    """每次感知到了什么，简要显示一下。"""
    nearby_str = "、".join(nearby) if nearby else "空无一人"
    console.print(
        f"  📍 [bold]{location_name}[/bold] ({position[0]}, {position[1]}) · 周围：{nearby_str}"
    )


def print_welcome_prompt(server_url: str, device_id: str, panel_url: str | None) -> None:
    """连上后给用户一个 hint。"""
    body_lines = [
        f"你的龙虾 [bold cyan]{device_id}[/bold cyan] 已抵达龙虾小镇。",
        "",
        "[dim]控制[/dim]",
        "  打字 + 回车 → 让你的龙虾说这句话",
        "  /1 自由   /2 被动   /3 待命   /q 退出",
        "",
        "关闭此窗口 = 龙虾离开小镇",
    ]
    if panel_url:
        body_lines.append(f"\n网页面板： [link={panel_url}]{panel_url}[/link]")
    panel = Panel(
        Group(*[Text.from_markup(line) for line in body_lines]),
        border_style="green",
        padding=(1, 2),
    )
    console.print(panel)


_AUTONOMY_LABEL = {
    "auto": "🟢 自由",
    "passive": "🟡 被动",
    "manual": "🔴 待命",
}


def print_autonomy_changed(level: str) -> None:
    label = _AUTONOMY_LABEL.get(level, level)
    console.print(f"[bold magenta]→[/bold magenta] 主动性切换为 {label}")


def print_player_speak(content: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    console.print(f"[dim][{ts}][/dim] [bold magenta]你[/bold magenta]：{content}")


def _summarize_action(action: dict[str, Any]) -> str:
    atype = action.get("type")
    content = action.get("content")
    if atype == "speak" and isinstance(content, str):
        return f'说："{content}"'
    if atype == "move" and isinstance(content, dict):
        return f"走向 ({content.get('target_x')}, {content.get('target_y')})"
    if atype == "change_location" and isinstance(content, str):
        return f"前往 → {content}"
    if atype == "emote" and isinstance(content, str):
        return f"（{content}）"
    if atype == "idle":
        return "发呆"
    return ""
