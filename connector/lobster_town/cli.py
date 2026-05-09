"""
CLI 入口。命令：
  lobster-town start     : 一键入镇（连入 + 自动开浏览器，**推荐**）
  lobster-town connect   : 连入小镇（保留作进阶用法）
  lobster-town tell      : 一次性下指令
  lobster-town config    : 改名字 / 主动性 / 群聊策略
  lobster-town status    : 看当前龙虾健康度
  lobster-town whoami    : 显示本机身份
  lobster-town forget    : 清除本地身份（小心）
  lobster-town uninstall : 卸载 connector（venv + symlink）
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import webbrowser

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


# ---------------- 推荐入口：start ----------------


@main.command()
@click.option("--server", default=DEFAULT_SERVER, help="平台地址")
@click.option("--display-name", default=None, help="你的龙虾昵称（首次注册用）")
@click.option(
    "--invite-code",
    default=lambda: os.environ.get("LOBSTER_INVITE_CODE"),
    help="Beta 邀请码（也可用 LOBSTER_INVITE_CODE 环境变量）",
)
@click.option("--no-browser", is_flag=True, help="不自动打开浏览器")
@click.option("--verbose", "-v", is_flag=True)
def start(
    server: str,
    display_name: str | None,
    invite_code: str | None,
    no_browser: bool,
    verbose: bool,
) -> None:
    """🦞 一键进入龙虾小镇（连入 + 自动开浏览器）。

    跟 connect 比就两个差别：
      1. 自动注入 NO_PROXY，省去手填环境变量
      2. 注册成功后自动用默认浏览器打开你的小屋页面
    """
    # 1. 注入 NO_PROXY（用户可以用环境变量覆盖）
    from urllib.parse import urlparse

    host = urlparse(server).hostname or "www.aigameplay.fun"
    bare = host[4:] if host.startswith("www.") else host
    existing = os.environ.get("NO_PROXY", "")
    needed = {host, bare}
    extra = ",".join(needed - set(filter(None, existing.split(","))))
    if extra:
        os.environ["NO_PROXY"] = (existing + "," + extra).strip(",") if existing else extra
        os.environ["no_proxy"] = os.environ["NO_PROXY"]

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 2. 注册（同 connect 流程）
    identity, is_new = load_or_create_identity(server_url=server)
    register_body: dict = {
        "public_key_hex": identity.public_key_hex,
        "display_name": display_name or identity.device_id,
    }
    if invite_code:
        register_body["invite_code"] = invite_code

    console.print(f"[dim]→ 走向小镇入口 {server} ...[/dim]")
    try:
        resp = httpx.post(
            server.rstrip("/") + "/api/devices/register",
            json=register_body,
            timeout=15.0,
        )
        if resp.status_code == 403:
            print_error("登记被拒：可能需要邀请码。")
            console.print(
                "[dim]用 --invite-code <code> 或 LOBSTER_INVITE_CODE 环境变量。[/dim]\n"
                "[dim]找内测组织者拿邀请码。[/dim]"
            )
            sys.exit(1)
        resp.raise_for_status()
        data = resp.json()
        device_id = data["device_id"]
        if data.get("is_new"):
            console.print(f"[green]✓[/green] 已登记：{data['display_name']} ({device_id})")
        else:
            console.print(f"[green]✓[/green] 欢迎回来：{data['display_name']} ({device_id})")
    except (httpx.HTTPError, Exception) as e:
        print_error(f"登记失败：{e}")
        sys.exit(1)

    # 3. 自动开浏览器
    panel_url = f"{server.rstrip('/')}/?d={device_id}"
    if not no_browser:
        try:
            webbrowser.open(panel_url)
            console.print(f"[dim]→ 浏览器已打开：{panel_url}[/dim]")
        except Exception:
            console.print(f"[dim]→ 浏览器打开失败，手动访问：{panel_url}[/dim]")

    # 4. 漂亮横幅
    print_banner(server, identity.device_id, is_new)
    console.print(
        f"\n[dim]按 Ctrl+C 下线（身份保留，下次直接 lobster-town start）[/dim]\n"
    )

    # 5. 跑 behavior loop
    adapter = None
    from lobster_town.deepseek_adapter import from_env as deepseek_from_env

    ds = deepseek_from_env()
    if ds:
        adapter = ds
        console.print("[dim]adapter: deepseek (LOBSTER_DEEPSEEK_KEY 已读取)[/dim]")

    try:
        asyncio.run(BehaviorLoop(identity, adapter=adapter, panel_base_url=panel_url).run())
    except KeyboardInterrupt:
        console.print("\n[dim]你的龙虾回小屋休息了 🏠[/dim]")


# ---------------- config ----------------


@main.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """看 / 改龙虾的小设置（名字 / 主动性 / 群聊策略）。

    不带子命令时 = 看当前配置；想改用 `lobster-town config set <key> <值>`。
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_show)


@config.command("show", hidden=True)
def config_show() -> None:
    """看当前配置（和裸 `lobster-town config` 等价）。"""
    if not DEVICE_FILE.exists():
        print_error("还没接入过龙虾小镇。先跑 lobster-town start。")
        sys.exit(1)
    identity, _ = load_or_create_identity(server_url=DEFAULT_SERVER)
    base = (identity.server_url or DEFAULT_SERVER).rstrip("/")

    console.print(f"🦞 [bold cyan]{identity.device_id}[/bold cyan]")
    console.print(f"   服务器：{base}")
    console.print(f"   身份文件：{DEVICE_FILE}")

    # 拉服务端的字段
    try:
        char = httpx.get(
            f"{base}/api/devices/{identity.device_id}",
            timeout=10.0,
        ).json()
        console.print(f"   名字：[bold]{char.get('display_name','?')}[/bold]")
        console.print(f"   位置：{char.get('current_location','?')}")
        console.print(f"   在线：{'🟢' if char.get('is_online') else '⚪'}")
    except Exception as e:
        console.print(f"   [dim]（拉服务端状态失败：{e}）[/dim]")

    # 群聊策略
    try:
        ts = httpx.get(
            f"{base}/api/devices/{identity.device_id}/topic-state",
            timeout=10.0,
        )
        if ts.status_code == 200:
            policy = ts.json().get("topic_join_policy", "?")
            label = {"skip": "🛑 不参与", "eager": "💬 自动加入"}.get(policy, policy)
            console.print(f"   话题策略：{label}")
    except Exception:
        pass


_VALID_AUTONOMY = {"auto", "passive", "manual"}
_VALID_POLICY = {"skip", "eager"}


@config.command("set")
@click.argument("key", type=click.Choice(["name", "autonomy", "policy"]))
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """改单条：lobster-town config set <name|autonomy|policy> <值>。

    示例：
      lobster-town config set autonomy passive
      lobster-town config set policy eager
      lobster-town config set name 小蓝
    """
    if not DEVICE_FILE.exists():
        print_error("还没接入过龙虾小镇。先跑 lobster-town start。")
        sys.exit(1)
    identity, _ = load_or_create_identity(server_url=DEFAULT_SERVER)
    base = (identity.server_url or DEFAULT_SERVER).rstrip("/")

    if key == "autonomy":
        if value not in _VALID_AUTONOMY:
            print_error(f"autonomy 取值必须是 {sorted(_VALID_AUTONOMY)} 之一")
            sys.exit(2)
        r = httpx.post(
            f"{base}/api/devices/{identity.device_id}/autonomy",
            json={"level": value},
            timeout=10.0,
        )
    elif key == "policy":
        if value not in _VALID_POLICY:
            print_error(f"policy 取值必须是 {sorted(_VALID_POLICY)} 之一")
            sys.exit(2)
        r = httpx.post(
            f"{base}/api/devices/{identity.device_id}/topic-policy",
            json={"policy": value},
            timeout=10.0,
        )
    elif key == "name":
        # 平台目前没有改 display_name 的端点；让用户走 forget 再 start
        print_error("改名字暂时要走：lobster-town forget → start --display-name <新名字>")
        console.print("[dim]注意：forget 会丢掉当前身份，重新生成 device_id。[/dim]")
        sys.exit(1)
    else:
        print_error(f"未知 key：{key}")
        sys.exit(2)

    if r.status_code == 200:
        console.print(f"[green]✓[/green] 已更新：{key} = {value}")
    else:
        print_error(f"服务端拒绝（HTTP {r.status_code}）：{r.text[:200]}")
        sys.exit(1)


# ---------------- status ----------------


@main.command()
def status() -> None:
    """看你的龙虾当前状态（位置、最近事件、未读邮件数）。"""
    if not DEVICE_FILE.exists():
        print_error("还没接入过龙虾小镇。先跑 lobster-town start。")
        sys.exit(1)
    identity, _ = load_or_create_identity(server_url=DEFAULT_SERVER)
    base = (identity.server_url or DEFAULT_SERVER).rstrip("/")

    try:
        char = httpx.get(f"{base}/api/devices/{identity.device_id}", timeout=10).json()
    except Exception as e:
        print_error(f"拉状态失败：{e}")
        sys.exit(1)

    console.print(f"\n🦞 [bold cyan]{char.get('display_name','?')}[/bold cyan] ({identity.device_id})")
    console.print(f"   位置：{char.get('current_location','?')}")
    console.print(f"   在线：{'🟢' if char.get('is_online') else '⚪ 不在线 (跑 lobster-town start)'}")

    # 邮箱未读
    try:
        unread = httpx.get(
            f"{base}/api/devices/{identity.device_id}/inbox/unread-count",
            timeout=10,
        )
        if unread.status_code == 200:
            n = unread.json().get("count", 0)
            badge = f"[bold red]{n}[/bold red]" if n > 0 else "[dim]0[/dim]"
            console.print(f"   邮箱未读：✉️ {badge}")
    except Exception:
        pass

    # 最近 5 条事件
    try:
        evs = httpx.get(
            f"{base}/api/devices/{identity.device_id}/recent-events?limit=5",
            timeout=10,
        ).json()
        if evs:
            console.print("\n   [bold]最近 5 条：[/bold]")
            for e in evs:
                t = (e.get("created_at") or "")[11:19]
                console.print(f"     [dim]{t}[/dim] {e.get('event_type','?')}")
    except Exception:
        pass


# ---------------- uninstall ----------------


@main.command()
@click.option("--keep-identity", is_flag=True, help="保留本地身份（默认会删）")
@click.confirmation_option(prompt="确定要卸载 lobster-town？")
def uninstall(keep_identity: bool) -> None:
    """卸载 connector（venv + symlink），可选保留身份文件。"""
    import shutil
    import subprocess
    from pathlib import Path

    home = Path.home()
    targets = [
        home / ".lobster-town-venv",
        home / ".local" / "bin" / "lobster-town",
    ]
    removed = []
    for p in targets:
        if p.exists() or p.is_symlink():
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed.append(str(p))
            except Exception as e:
                console.print(f"[yellow]⚠[/yellow] 删 {p} 失败：{e}")

    # pipx 模式装的话也尝试 uninstall
    try:
        subprocess.run(
            ["pipx", "uninstall", "lobster-town"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass

    if not keep_identity and (home / ".lobster-town").exists():
        try:
            shutil.rmtree(home / ".lobster-town")
            removed.append(str(home / ".lobster-town"))
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] 删身份文件失败：{e}")

    if removed:
        console.print("[green]✓[/green] 已删除：")
        for p in removed:
            console.print(f"   {p}")
    else:
        console.print("[yellow]没找到任何要删的内容。[/yellow]")

    console.print("\n[dim]如果之前给 .zshrc/.bashrc 加过 PATH 行，要自己删掉那一行。[/dim]")


if __name__ == "__main__":
    main()
