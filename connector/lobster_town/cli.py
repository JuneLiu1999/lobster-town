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
@click.argument("message", nargs=-1, required=True)
@click.option(
    "--server",
    default=DEFAULT_SERVER,
    help="平台地址（默认从 LOBSTER_SERVER 或本机已存的 device.json）",
)
@click.option("--quiet", "-q", is_flag=True, help="只输出错误，成功不打印")
def tell(message: tuple[str, ...], server: str, quiet: bool) -> None:
    """临时给你的龙虾下一条指令（无需常驻 Connector）。

    例：
        lobster-town tell 去任务中心
        lobster-town tell 找老板娘问问有没有任务
        lobster-town tell -- "去喷泉边发会儿呆"

    本质上是一次性 HTTP 调用，等同于在 Connector 终端里打字 + 回车。
    需要本机已经至少 connect 过一次（拿到 device_id 写到 ~/.lobster-town）。
    """
    if not DEVICE_FILE.exists():
        print_error("本机还没接入过龙虾小镇。先跑一次 lobster-town connect。")
        sys.exit(1)

    text = " ".join(message).strip()
    if not text:
        print_error("内容是空的。")
        sys.exit(1)

    identity, _ = load_or_create_identity(server_url=server)
    base = (identity.server_url or server).rstrip("/")

    try:
        resp = httpx.post(
            f"{base}/api/devices/{identity.device_id}/directive",
            json={"content": text},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        print_error(f"发送失败：{e}")
        sys.exit(1)

    if resp.status_code != 200:
        print_error(f"服务器拒绝（HTTP {resp.status_code}）：{resp.text[:200]}")
        sys.exit(1)

    if not quiet:
        console.print(
            f"[bold green]✓[/bold green] 已下达指令：[bold magenta]{text}[/bold magenta]\n"
            f"[dim]Connector 在跑的话，龙虾下一 tick 会理解这条指令并执行。[/dim]"
        )


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
