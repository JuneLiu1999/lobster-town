"""
CLI 入口。命令：
  lobster-town start     : 一键入镇（连入 + 自动开浏览器，**推荐**）
  lobster-town setup     : 配置龙虾的大脑（OpenClaw 或 API key 直连）
  lobster-town connect   : 连入小镇（保留作进阶用法）
  lobster-town tell      : 一次性下指令
  lobster-town config    : 改名字 / 主动性 / 大脑配置
  lobster-town status    : 看当前龙虾健康度
  lobster-town whoami    : 显示本机身份
  lobster-town forget    : 清除本地身份（小心）
  lobster-town uninstall : 卸载 connector（venv + symlink）
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import click
import httpx

from lobster_town import __version__
from lobster_town.behavior_loop import BehaviorLoop
from lobster_town.identity import (
    CONFIG_DIR,
    DEVICE_FILE,
    PRIVATE_KEY_FILE,
    forget_identity,
    load_or_create_identity,
)
from lobster_town.ui import console, print_banner, print_error


# ----- 守护进程相关路径（跟 device.json 同目录）-----
PID_FILE = CONFIG_DIR / "connector.pid"
LOG_FILE = CONFIG_DIR / "connector.log"


def _running_pid() -> int | None:
    """返回正在跑的 connector 守护进程 PID；没在跑返回 None；
    PID 文件残留但进程已死会自动清掉文件。"""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)  # signal 0 = 仅探测进程是否存在
        return pid
    except ProcessLookupError:
        # 进程没了，清残留 PID 文件
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return None
    except PermissionError:
        # 进程在但不是当前用户的（罕见）
        return pid


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

    adapter = _resolve_adapter()

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
        lobster-town tell 去酒馆
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
            headers=_authed_headers(base, identity),
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


def _inject_noproxy(server: str) -> None:
    """把 server 域名加进 NO_PROXY，免得用户被本机/公司代理拦。"""
    from urllib.parse import urlparse

    host = urlparse(server).hostname or "www.aigameplay.fun"
    bare = host[4:] if host.startswith("www.") else host
    existing = os.environ.get("NO_PROXY", "")
    needed = {host, bare}
    extra = ",".join(needed - set(filter(None, existing.split(","))))
    if extra:
        os.environ["NO_PROXY"] = (existing + "," + extra).strip(",") if existing else extra
        os.environ["no_proxy"] = os.environ["NO_PROXY"]


def _resolve_adapter():
    """按优先级选 adapter：config.json direct > env LOBSTER_DEEPSEEK_KEY（已废弃）> 默认 openclaw。"""
    import shutil

    from lobster_town.adapter_config import load_config

    cfg = load_config()
    adapter_type = cfg.get("adapter", "openclaw")

    if adapter_type == "direct":
        from lobster_town.generic_llm_adapter import from_config

        adapter = from_config()
        if adapter:
            console.print(
                f"🧠 大脑：[bold]direct[/bold] · {adapter.model} [dim]({adapter.base_url})[/dim]"
            )
            return adapter
        console.print(
            "[yellow]⚠ adapter=direct 但 LLM 配置不全，回退到 OpenClaw。"
            "跑 [bold]lobster-town setup[/bold] 补全配置。[/yellow]"
        )

    # 已废弃的 env var 方式（仅兼容保留，提示迁移）
    from lobster_town.deepseek_adapter import from_env as deepseek_from_env

    ds = deepseek_from_env()
    if ds:
        console.print(
            "🧠 大脑：[bold]deepseek[/bold]（来自 LOBSTER_DEEPSEEK_KEY 环境变量）\n"
            "[yellow]⚠ 环境变量方式已废弃，请迁移到 [bold]lobster-town setup[/bold]"
            "（配置会落盘，不用每次 export）[/yellow]"
        )
        return ds

    console.print("🧠 大脑：[bold]OpenClaw[/bold]（本机 openclaw 子进程）")
    if shutil.which("openclaw") is None:
        console.print(
            "[yellow]⚠ 本机没找到 openclaw 命令 —— 龙虾不会自主行动（只接你打字的指令）。\n"
            "  · 装 OpenClaw：https://docs.openclaw.ai/zh-CN/install\n"
            "  · 或改用 API key 直连：[bold]lobster-town setup[/bold][/yellow]"
        )
    return None


def _post_register(server: str, body: dict) -> httpx.Response:
    return httpx.post(
        server.rstrip("/") + "/api/devices/register",
        json=body,
        timeout=15.0,
    )


def _authed_headers(server: str, identity) -> dict[str, str]:
    """给 CLI 命令拉一次性 token 并组装 Authorization 头。
    失败返回空 dict（让上层调用走未鉴权路径，可能拿到 401）。
    """
    token = _request_browser_token(server, identity)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _request_browser_token(server: str, identity) -> str | None:
    """challenge → sign → exchange → 返回 24h token；任何步骤失败返回 None。

    浏览器侧用这个 token 调敏感 API（recent-events / inbox / say / directive 等）。
    没 token 也能进 cottage 看公开布局，但所有"读自己 thought / 发指令"都会 401。
    """
    base = server.rstrip("/")
    try:
        r = httpx.post(
            f"{base}/api/devices/{identity.device_id}/auth/challenge",
            json={},
            timeout=10,
        )
        r.raise_for_status()
        nonce = r.json()["nonce"]
    except (httpx.HTTPError, KeyError, Exception) as e:
        console.print(f"[dim]token: challenge 失败（{e}）[/dim]")
        return None
    sig = identity.sign(nonce.encode()).hex()
    try:
        r2 = httpx.post(
            f"{base}/api/devices/{identity.device_id}/auth/token",
            json={"nonce": nonce, "signature": sig},
            timeout=10,
        )
        r2.raise_for_status()
        return r2.json()["token"]
    except (httpx.HTTPError, KeyError, Exception) as e:
        console.print(f"[dim]token: exchange 失败（{e}）[/dim]")
        return None


def _register_with_invite_retry(
    server: str, body: dict, initial_invite: str | None
) -> dict:
    """注册流程：
      - 没传邀请码 → 直接试一次
      - 服务端 403 = 需要邀请码 → 交互式询问 → 重试
      - 输 3 次还失败 → 抛 RuntimeError
    """
    if initial_invite:
        body["invite_code"] = initial_invite

    resp = _post_register(server, body)
    if resp.status_code != 403:
        resp.raise_for_status()
        return resp.json()

    # 403：可能是首次进 + 没带邀请码，也可能是邀请码错。
    # 给最多 3 次机会让用户输入。
    for attempt in range(1, 4):
        if attempt == 1:
            console.print(
                "\n[bold yellow]🎟  这是你第一次进入小镇，需要邀请码。[/bold yellow]"
            )
            console.print(
                "[dim]  · 找内测组织者拿；[/dim]\n"
                "[dim]  · 或在 GitHub 仓库 Issues 留言："
                "https://github.com/JuneLiu1999/lobster-town/issues[/dim]"
            )
        else:
            console.print(
                f"[yellow]邀请码无效，再试一次（{attempt}/3）[/yellow]"
            )
        try:
            invite = click.prompt("邀请码", default="", show_default=False).strip()
        except (click.Abort, EOFError, KeyboardInterrupt):
            raise RuntimeError("用户取消输入")
        if not invite:
            raise RuntimeError("没输入邀请码")
        body["invite_code"] = invite
        resp = _post_register(server, body)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 403:
            resp.raise_for_status()
    raise RuntimeError("邀请码 3 次都不对；找内测组织者确认")


@main.command()
@click.option("--server", default=DEFAULT_SERVER, help="平台地址")
@click.option("--display-name", default=None, help="你的龙虾昵称（首次注册用）")
@click.option(
    "--invite-code",
    default=lambda: os.environ.get("LOBSTER_INVITE_CODE"),
    help="Beta 邀请码（也可用 LOBSTER_INVITE_CODE 环境变量）",
)
@click.option("--no-browser", is_flag=True, help="不自动打开浏览器")
@click.option("--foreground", "-f", is_flag=True, help="前台跑监视器（旧行为，调试用）")
@click.option("--verbose", "-v", is_flag=True)
def start(
    server: str,
    display_name: str | None,
    invite_code: str | None,
    no_browser: bool,
    foreground: bool,
    verbose: bool,
) -> None:
    """🦞 一键入镇（连入 + 开浏览器 + 后台守护，终端立即归还）。

    流程：
      1. 注入 NO_PROXY（绕开本机代理）
      2. 注册（拿邀请码或老身份）
      3. 自动打开浏览器
      4. fork 守护进程跑 BehaviorLoop，**终端立即归还**
      5. 你能继续跑 lobster-town status / config / tell 等命令

    后续控制：
      lobster-town status   看在不在线 + 最近事件
      lobster-town logs     看 connector 内部日志（含内心独白）
      lobster-town stop     下线
      lobster-town start    再次入镇（已在线时只重新打开浏览器）

    调试用：加 --foreground / -f 走前台监视器（按 Ctrl+C 退出）。
    """
    _inject_noproxy(server)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 已经在跑？签发新 token + 重开浏览器
    pid = _running_pid()
    if pid:
        console.print(f"[yellow]🦞 你的龙虾已经在小镇里 (PID {pid})[/yellow]")
        try:
            identity, _ = load_or_create_identity(server_url=server)
            browser_token = _request_browser_token(server, identity)
            panel_url = f"{server.rstrip('/')}/?d={identity.device_id}"
            if browser_token:
                panel_url += f"&t={browser_token}"
        except Exception:
            panel_url = server
        if not no_browser:
            try:
                webbrowser.open(panel_url)
                console.print(f"[dim]浏览器已打开（已刷新 token）[/dim]")
            except Exception:
                pass
        console.print(
            "\n[dim]看状态: lobster-town status  ·  停止: lobster-town stop[/dim]"
        )
        return

    # 注册（前台，让用户立即看到错误）
    identity, is_new = load_or_create_identity(server_url=server)
    register_body: dict = {
        "public_key_hex": identity.public_key_hex,
        "display_name": display_name or identity.device_id,
    }
    if invite_code:
        register_body["invite_code"] = invite_code

    console.print(f"[dim]→ 走向小镇入口 {server} ...[/dim]")
    try:
        data = _register_with_invite_retry(server, register_body, invite_code)
    except (httpx.HTTPError, Exception) as e:
        print_error(f"登记失败：{e}")
        sys.exit(1)
    device_id = data["device_id"]
    if data.get("is_new"):
        console.print(f"[green]✓[/green] 已登记：{data['display_name']} ({device_id})")
    else:
        console.print(f"[green]✓[/green] 欢迎回来：{data['display_name']} ({device_id})")

    # 拿浏览器会话 token —— 用 Ed25519 私钥签 challenge 换的，
    # 让网页能读 thought / inbox 这些敏感数据；没 token 别人即使有 device_id
    # 也只看公开静态页。
    browser_token = _request_browser_token(server, identity)
    panel_url = f"{server.rstrip('/')}/?d={device_id}"
    if browser_token:
        panel_url += f"&t={browser_token}"

    # 开浏览器
    if not no_browser:
        try:
            webbrowser.open(panel_url)
            # 别在终端打出 token 全文 —— 防截屏泄露
            short_url = f"{server.rstrip('/')}/?d={device_id}" + (
                "&t=…" if browser_token else ""
            )
            console.print(f"[dim]→ 浏览器已打开：{short_url}[/dim]")
        except Exception:
            console.print(f"[dim]→ 浏览器打开失败，手动访问：{panel_url}[/dim]")

    # 前台模式（调试用）：跑 BehaviorLoop 占着终端
    if foreground:
        print_banner(server, identity.device_id, is_new)
        console.print("\n[dim]前台监视器模式（Ctrl+C 退出）[/dim]\n")
        adapter = _resolve_adapter()
        try:
            asyncio.run(
                BehaviorLoop(identity, adapter=adapter, panel_base_url=panel_url).run()
            )
        except KeyboardInterrupt:
            console.print("\n[dim]你的龙虾回小屋休息了 🏠[/dim]")
        return

    # ---- 后台守护进程模式（默认）----
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log_fd = open(LOG_FILE, "ab")
    sep = f"\n==== start {time.strftime('%Y-%m-%d %H:%M:%S')} (server={server}) ====\n"
    log_fd.write(sep.encode())
    log_fd.flush()

    proc_env = dict(os.environ)
    proc_env["NO_PROXY"] = os.environ.get("NO_PROXY", "")
    proc_env["no_proxy"] = os.environ.get("no_proxy", "")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "lobster_town.cli",
            "_daemon-run",
            "--server",
            server,
            "--panel-url",
            panel_url,
        ],
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
        env=proc_env,
    )
    PID_FILE.write_text(str(proc.pid))

    # 给守护进程一点时间检查启动错误（不阻塞太久）
    time.sleep(0.5)
    if proc.poll() is not None:
        print_error(f"守护进程启动失败（exit {proc.returncode}）")
        console.print(f"[dim]看日志：lobster-town logs[/dim]")
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        sys.exit(1)

    print_banner(server, identity.device_id, is_new)
    console.print(
        f"\n[bold green]✓[/bold green] 龙虾在后台运行 (PID {proc.pid})"
    )
    console.print(f"  网页:  [cyan]{panel_url}[/cyan]")
    console.print(f"  状态:  [bold]lobster-town status[/bold]")
    console.print(f"  日志:  [bold]lobster-town logs[/bold]")
    console.print(f"  停止:  [bold]lobster-town stop[/bold]")
    console.print()


@main.command(name="_daemon-run", hidden=True)
@click.option("--server", default=DEFAULT_SERVER)
@click.option("--panel-url", default="")
def _daemon_run(server: str, panel_url: str) -> None:
    """[内部] start 命令 fork 出来跑后台 BehaviorLoop 用，用户不该直接调。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    identity, _ = load_or_create_identity(server_url=server)
    adapter = _resolve_adapter()
    try:
        asyncio.run(
            BehaviorLoop(
                identity,
                adapter=adapter,
                panel_base_url=panel_url or server,
            ).run()
        )
    except KeyboardInterrupt:
        pass


@main.command()
def stop() -> None:
    """🛑 让你的龙虾下线（身份保留）。"""
    pid = _running_pid()
    if not pid:
        console.print("[yellow]龙虾不在小镇里。[/yellow]")
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        console.print("[yellow]进程已不存在。[/yellow]")
        return
    except PermissionError as e:
        print_error(f"没权限杀 PID {pid}：{e}")
        sys.exit(1)

    # 等最多 3 秒；不退就 KILL
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        PID_FILE.unlink()
    except OSError:
        pass
    console.print(
        "[green]✓[/green] 龙虾已下线（身份保留，下次 lobster-town start）"
    )


@main.command()
@click.option("-n", "--lines", default=30, help="显示最近多少行（默认 30）")
@click.option("-f", "--follow", is_flag=True, help="实时跟踪")
def logs(lines: int, follow: bool) -> None:
    """📜 看 connector 后台日志（含内心独白等）。"""
    if not LOG_FILE.exists():
        console.print(
            "[yellow]还没有日志。先跑 lobster-town start。[/yellow]"
        )
        return
    if follow:
        try:
            subprocess.call(["tail", "-f", "-n", str(lines), str(LOG_FILE)])
        except KeyboardInterrupt:
            pass
        return
    try:
        with LOG_FILE.open("r", errors="replace") as f:
            buf = f.readlines()
    except OSError as e:
        print_error(f"读日志失败：{e}")
        sys.exit(1)
    for line in buf[-lines:]:
        sys.stdout.write(line)


# ---------------- setup（大脑配置向导） ----------------


# 常见 OpenAI 兼容服务预设：名字 → (base_url, 默认模型, 拿 key 的地址)
_LLM_PRESETS: list[tuple[str, str, str, str]] = [
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat", "https://platform.deepseek.com"),
    ("Moonshot Kimi", "https://api.moonshot.cn/v1", "moonshot-v1-8k", "https://platform.moonshot.cn"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini", "https://platform.openai.com"),
]


def _check_direct_llm() -> bool:
    """用当前 config 建 direct adapter，发一次真实请求验证；打印结果。

    配置不全返回 False（不算错误，只提示）；配置全但验证失败也返回 False。
    """
    from lobster_town.generic_llm_adapter import from_config

    adapter = from_config()
    if adapter is None:
        console.print(
            "[dim]（llm-base-url / llm-api-key / llm-model 还没配齐，配齐后会自动验证）[/dim]"
        )
        return False

    console.print(f"[dim]验证 {adapter.base_url} · {adapter.model} ...[/dim]")

    async def _run() -> tuple[bool, str]:
        try:
            return await adapter.health_check()
        finally:
            await adapter.close()

    try:
        ok, detail = asyncio.run(_run())
    except Exception as e:  # 校验本身不该让 CLI 崩
        ok, detail = False, f"验证过程出错：{e}"

    if ok:
        console.print(f"[green]✓[/green] {detail}")
    else:
        print_error(detail)
    return ok


@main.command()
def setup() -> None:
    """🧠 交互式配置龙虾的大脑（OpenClaw 或 API key 直连）。"""
    import shutil

    from lobster_town.adapter_config import load_config as _load_cfg, set_value

    cfg = _load_cfg()
    current = cfg.get("adapter", "openclaw")
    console.print(f"\n🧠 [bold]给你的龙虾选一个大脑[/bold]  [dim]（当前：{current}）[/dim]\n")
    console.print("  [bold]1[/bold]. OpenClaw     —— 用本机的 OpenClaw（你的 Claude 账号出智力）")
    console.print("  [bold]2[/bold]. API key 直连 —— 填一个 OpenAI 兼容 API（DeepSeek / Kimi / OpenAI / 自建）\n")

    choice = click.prompt("选哪个", type=click.Choice(["1", "2"]), show_choices=False)

    if choice == "1":
        set_value("adapter", "openclaw")
        console.print("[green]✓[/green] 已设置：adapter = openclaw")
        if shutil.which("openclaw") is None:
            console.print(
                "[yellow]⚠ 本机还没装 OpenClaw。装好后 lobster-town start 龙虾就活了：\n"
                "  https://docs.openclaw.ai/zh-CN/install[/yellow]"
            )
        else:
            console.print("[dim]本机已有 openclaw 命令，直接 lobster-town start 即可。[/dim]")
        return

    # ---- 直连路线 ----
    console.print("\n选一个服务商（都是 OpenAI 兼容接口）：\n")
    for i, (name, url, model, key_url) in enumerate(_LLM_PRESETS, start=1):
        console.print(f"  [bold]{i}[/bold]. {name:<14} [dim]{url} · 拿 key：{key_url}[/dim]")
    console.print(f"  [bold]{len(_LLM_PRESETS) + 1}[/bold]. 自定义        [dim]任何 OpenAI 兼容端点（OpenRouter / Ollama / vLLM ...）[/dim]\n")

    n_choices = [str(i) for i in range(1, len(_LLM_PRESETS) + 2)]
    pick = int(click.prompt("服务商", type=click.Choice(n_choices), show_choices=False))

    if pick <= len(_LLM_PRESETS):
        _, base_url, default_model, _ = _LLM_PRESETS[pick - 1]
        base_url = click.prompt("API 地址", default=base_url)
    else:
        base_url = click.prompt("API 地址（一般以 /v1 结尾）").strip()
        default_model = ""

    api_key = click.prompt("API Key", hide_input=True).strip()
    model = click.prompt("模型名", default=default_model or None).strip()

    set_value("adapter", "direct")
    set_value("llm_base_url", base_url.rstrip("/"))
    set_value("llm_api_key", api_key)
    set_value("llm_model", model)
    console.print("[green]✓[/green] 配置已保存到 ~/.lobster-town/config.json（key 权限 600）\n")

    if _check_direct_llm():
        console.print("\n🦞 大脑接好了！跑 [bold]lobster-town start[/bold] 入镇。")
    else:
        console.print(
            "\n[yellow]配置已保存但验证没通过。改单项用：\n"
            "  lobster-town config set llm-api-key <key>\n"
            "  lobster-town config set llm-model <模型名>\n"
            "或重新跑 lobster-town setup[/yellow]"
        )
        sys.exit(1)


# ---------------- config ----------------


@main.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """看 / 改龙虾的小设置（名字 / 主动性 / 大脑配置）。

    不带子命令时 = 看当前配置；想改用 `lobster-town config set <key> <值>`。
    大脑配置推荐用交互式向导：`lobster-town setup`。
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

    # adapter 配置
    from lobster_town.adapter_config import load_config as _load_adapter_cfg, mask_key

    acfg = _load_adapter_cfg()
    adapter_type = acfg.get("adapter", "openclaw")
    console.print(f"   adapter：[bold]{adapter_type}[/bold]")
    if adapter_type == "direct":
        console.print(f"   llm-base-url：{acfg.get('llm_base_url') or '[dim]未设置[/dim]'}")
        console.print(f"   llm-api-key：{mask_key(acfg.get('llm_api_key', ''))}")
        console.print(f"   llm-model：{acfg.get('llm_model') or '[dim]未设置[/dim]'}")



_VALID_AUTONOMY = {"auto", "passive", "manual"}


_VALID_ADAPTERS = {"openclaw", "direct"}
_LOCAL_CONFIG_KEYS = {"adapter", "llm-base-url", "llm-api-key", "llm-model"}
_ALL_CONFIG_KEYS = {"name", "autonomy"} | _LOCAL_CONFIG_KEYS


@config.command("set")
@click.argument("key", type=click.Choice(sorted(_ALL_CONFIG_KEYS)))
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """改单条：lobster-town config set <key> <值>。

    服务端配置：
      lobster-town config set autonomy passive
      lobster-town config set name 小蓝

    本地 adapter 配置：
      lobster-town config set adapter direct
      lobster-town config set llm-base-url https://api.deepseek.com/v1
      lobster-town config set llm-api-key sk-...
      lobster-town config set llm-model deepseek-chat
    """
    # 本地配置（adapter / LLM）
    if key in _LOCAL_CONFIG_KEYS:
        if key == "adapter" and value not in _VALID_ADAPTERS:
            print_error(f"adapter 取值必须是 {sorted(_VALID_ADAPTERS)} 之一")
            sys.exit(2)
        from lobster_town.adapter_config import load_config as _load_cfg, set_value, mask_key

        set_value(key, value)
        display = mask_key(value) if key == "llm-api-key" else value
        console.print(f"[green]✓[/green] 已保存：{key} = {display}")

        # 即时校验：direct 模式配齐三件套后立刻发一次真实请求，
        # 别让用户等到 start 之后才发现 key 填错了
        if _load_cfg().get("adapter") == "direct" and key != "adapter":
            if not _check_direct_llm():
                sys.exit(1)
        elif key == "adapter" and value == "direct":
            _check_direct_llm()
        return

    # 服务端配置
    if not DEVICE_FILE.exists():
        print_error("还没接入过龙虾小镇。先跑 lobster-town start。")
        sys.exit(1)
    identity, _ = load_or_create_identity(server_url=DEFAULT_SERVER)
    base = (identity.server_url or DEFAULT_SERVER).rstrip("/")

    headers = _authed_headers(base, identity)
    if key == "autonomy":
        if value not in _VALID_AUTONOMY:
            print_error(f"autonomy 取值必须是 {sorted(_VALID_AUTONOMY)} 之一")
            sys.exit(2)
        r = httpx.post(
            f"{base}/api/devices/{identity.device_id}/autonomy",
            json={"level": value},
            headers=headers,
            timeout=10.0,
        )
    elif key == "name":
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
    """看你的龙虾当前状态（位置、最近事件）。"""
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

    # 守护进程
    pid = _running_pid()
    if pid:
        console.print(f"   守护进程：🟢 在跑 (PID {pid})")
    else:
        console.print(f"   守护进程：⚪ 没在跑（lobster-town start 启动）")

    # 拉一次 token 调敏感接口
    headers = _authed_headers(base, identity)

    # 最近 5 条事件
    try:
        evs = httpx.get(
            f"{base}/api/devices/{identity.device_id}/recent-events?limit=5",
            headers=headers,
            timeout=10,
        ).json()
        if isinstance(evs, list) and evs:
            console.print("\n   [bold]最近 5 条：[/bold]")
            for e in evs:
                t = (e.get("created_at") or "")[11:19]
                console.print(f"     [dim]{t}[/dim] {e.get('event_type','?')}")
    except Exception:
        pass


# ---------------- uninstall ----------------


@main.command()
@click.option(
    "--purge",
    is_flag=True,
    help="同时删本地身份（device.json + private_key.bin）。默认会保留——身份不可恢复，谨慎。",
)
@click.confirmation_option(prompt="确定要卸载 lobster-town？")
def uninstall(purge: bool) -> None:
    """卸载 connector（venv + symlink）。**默认保留身份**——下次重装直接 lobster-town start 即可恢复。"""
    import shutil

    # 先停掉守护进程（如果在跑）
    pid = _running_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            PID_FILE.unlink()
        except OSError:
            pass

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

    if purge and (home / ".lobster-town").exists():
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
