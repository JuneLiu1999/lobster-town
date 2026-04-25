"""
CLI 入口。命令：
  lobster-town connect   : 连入小镇
  lobster-town whoami    : 显示本机身份
  lobster-town forget    : 清除本地身份（小心）
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import click
import httpx

from lobster_town import __version__
from lobster_town.behavior_loop import BehaviorLoop
from lobster_town.identity import (
    DEVICE_FILE,
    PRIVATE_KEY_FILE,
    forget_identity,
    load_or_create_identity,
)
from lobster_town.ui import console, print_banner, print_error


DEFAULT_SERVER = os.environ.get("LOBSTER_SERVER") or "https://www.aigameplay.fun"
DEFAULT_PANEL_BASE = os.environ.get("LOBSTER_PANEL") or "https://www.aigameplay.fun/panel"


@click.group()
@click.version_option(__version__)
def main() -> None:
    """🦞 把你的 OpenClaw 接入龙虾小镇。"""
    pass


@main.command()
@click.option("--server", default=DEFAULT_SERVER, help="平台地址")
@click.option("--panel", default=DEFAULT_PANEL_BASE, help="网页管理面板地址")
@click.option("--display-name", default=None, help="你的龙虾昵称（首次注册用）")
@click.option(
    "--invite-code",
    default=lambda: os.environ.get("LOBSTER_INVITE_CODE"),
    help="Beta 阶段邀请码（也可以用 LOBSTER_INVITE_CODE 环境变量）",
)
@click.option("--verbose", "-v", is_flag=True, help="输出详细日志")
def connect(
    server: str,
    panel: str,
    display_name: str | None,
    invite_code: str | None,
    verbose: bool,
) -> None:
    """进入龙虾小镇。"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    identity, is_new = load_or_create_identity(server_url=server)

    # 每次启动都登记一次（幂等：服务器端按公钥去重）。这样即使本地身份还在
    # 但服务器 DB 被清掉，也能自动恢复。
    register_body: dict = {
        "public_key_hex": identity.public_key_hex,
        "display_name": display_name or identity.device_id,
    }
    if invite_code:
        register_body["invite_code"] = invite_code

    try:
        resp = httpx.post(
            server.rstrip("/") + "/api/devices/register",
            json=register_body,
            timeout=15.0,
        )
        if resp.status_code == 403:
            print_error("登记被拒：可能需要邀请码。用 --invite-code <code> 或设 LOBSTER_INVITE_CODE 环境变量。")
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
        if data.get("is_new"):
            console.print(
                f"[green]✓[/green] 已登记为小镇居民：{data['display_name']} ({data['device_id']})"
            )
    except (httpx.HTTPError, Exception) as e:
        print_error(f"登记失败：{e}")
        sys.exit(1)

    print_banner(server, identity.device_id, is_new)

    # 临时：支持通过 env var 切换 DeepSeek adapter（测试用）
    adapter = None
    from lobster_town.deepseek_adapter import from_env as deepseek_from_env
    ds = deepseek_from_env()
    if ds:
        adapter = ds
        console.print("[dim]adapter: deepseek (LOBSTER_DEEPSEEK_KEY 已读取)[/dim]")

    try:
        asyncio.run(BehaviorLoop(identity, adapter=adapter, panel_base_url=panel).run())
    except KeyboardInterrupt:
        console.print("\n[dim]你的龙虾回小屋休息了 🏠[/dim]")


@main.command()
def whoami() -> None:
    """显示本机的龙虾身份。"""
    if not DEVICE_FILE.exists():
        console.print("[yellow]还没接入过龙虾小镇。运行 lobster-town connect 开始。[/yellow]")
        return

    from lobster_town.identity import load_or_create_identity

    identity, is_new = load_or_create_identity(server_url=DEFAULT_SERVER)
    console.print(f"🦞 Device ID: [bold cyan]{identity.device_id}[/bold cyan]")
    console.print(f"   公钥：     {identity.public_key_hex}")
    console.print(f"   服务器：   {identity.server_url}")
    console.print(f"   身份文件： {DEVICE_FILE}")
    console.print(f"   私钥文件： {PRIVATE_KEY_FILE}")


@main.command()
@click.confirmation_option(prompt="确定要删除本地身份？这会让你的龙虾永远失去现在的身份。")
def forget() -> None:
    """清除本机身份（小心，不可恢复）。"""
    forget_identity()
    console.print("[yellow]本地身份已清除。[/yellow]")


if __name__ == "__main__":
    main()
