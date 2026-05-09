#!/usr/bin/env sh
# 🦞 龙虾小镇 Connector 安装器
#
# 一行装：
#   curl -fsSL https://raw.githubusercontent.com/JuneLiu1999/lobster-town/main/scripts/install.sh | sh
#
# 想看完再装：
#   curl -fsSL https://raw.githubusercontent.com/JuneLiu1999/lobster-town/main/scripts/install.sh > install.sh
#   less install.sh && sh install.sh
#
# 装完会得到 lobster-town 命令；运行 lobster-town start 即可入镇。

set -eu

# ---------- 配色 / 输出 ----------
# 尊重 NO_COLOR 习惯；非 tty 时也禁色
if [ -n "${NO_COLOR:-}" ] || ! [ -t 1 ]; then
    C_RESET="" C_DIM="" C_BOLD="" C_RED="" C_GRN="" C_YEL="" C_CYAN="" C_MAG=""
else
    C_RESET="\033[0m"; C_DIM="\033[2m"; C_BOLD="\033[1m"
    C_RED="\033[1;31m"; C_GRN="\033[1;32m"; C_YEL="\033[1;33m"
    C_CYAN="\033[1;36m"; C_MAG="\033[1;35m"
fi

p()    { printf '%b\n' "$*"; }
ok()   { printf '      %b✓%b %s\n' "$C_GRN" "$C_RESET" "$1"; }
warn() { printf '      %b⚠%b %s\n' "$C_YEL" "$C_RESET" "$1"; }
fail() { printf '      %b✗%b %s\n' "$C_RED" "$C_RESET" "$1"; }
step() { printf '\n%b[%s/%s]%b %s\n' "$C_CYAN" "$1" "$STEP_TOTAL" "$C_RESET" "$2"; }

die() {
    printf '\n%b❌ 安装失败：%s%b\n' "$C_RED" "$1" "$C_RESET" >&2
    [ -n "${2:-}" ] && printf '%b   建议：%s%b\n' "$C_DIM" "$2" "$C_RESET" >&2
    exit 1
}

# Stdin 是 curl 时，要从 /dev/tty 读才能交互
ask() {
    # ask "<question>" "<default y|n>"  →  return 0=yes 1=no
    _q="$1"; _default="${2:-y}"
    if [ "$_default" = "y" ]; then _hint="[Y/n]"; else _hint="[y/N]"; fi
    if [ -e /dev/tty ]; then
        printf '      %b❓%b %s %s: ' "$C_MAG" "$C_RESET" "$_q" "$_hint"
        read _ans </dev/tty || _ans=""
    else
        # 完全非交互（如 docker build 没 tty）：按 default 走
        _ans=""
    fi
    case "$_ans" in
        y|Y|yes|YES) return 0 ;;
        n|N|no|NO)   return 1 ;;
        *)           [ "$_default" = "y" ] && return 0 || return 1 ;;
    esac
}

# ---------- 头 ----------
clear || true
p ""
p "    ${C_RED}🦞${C_RESET}  ${C_BOLD}龙虾小镇 · Connector 安装器${C_RESET}"
p "    ${C_DIM}Lobster Town · github.com/JuneLiu1999/lobster-town${C_RESET}"
p ""

STEP_TOTAL=6

# ---------- [1/6] OS ----------
step 1 "检测系统"
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
    Darwin) OS_PRETTY="macOS" ;;
    Linux)  OS_PRETTY="Linux" ;;
    *) die "暂不支持 $OS（目前只测过 macOS / Linux）" "Windows 用户请用 WSL 或等下个版本" ;;
esac
ok "$OS_PRETTY ($ARCH)"

# 探包管理器（用于装 Python / pipx 时给建议命令）
PKG_MGR=""
PKG_INSTALL=""
if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    PKG_MGR="brew"; PKG_INSTALL="brew install"
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MGR="apt"; PKG_INSTALL="sudo apt-get install -y"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"; PKG_INSTALL="sudo dnf install -y"
elif command -v pacman >/dev/null 2>&1; then
    PKG_MGR="pacman"; PKG_INSTALL="sudo pacman -S --noconfirm"
fi

# ---------- [2/6] Python ----------
step 2 "检测 Python (≥ 3.9)"

check_python() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

PYTHON_BIN=""
for cmd in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && check_python "$cmd"; then
        PYTHON_BIN="$cmd"; break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    fail "未找到 Python 3.9+"
    if [ -n "$PKG_MGR" ]; then
        case "$PKG_MGR" in
            brew) _cmd="brew install python" ;;
            apt)  _cmd="sudo apt-get install -y python3 python3-venv python3-pip" ;;
            dnf)  _cmd="sudo dnf install -y python3 python3-pip" ;;
            pacman) _cmd="sudo pacman -S --noconfirm python python-pip" ;;
        esac
        if ask "通过 $PKG_MGR 装 Python？($_cmd)" y; then
            sh -c "$_cmd" || die "装 Python 失败" "手动跑：$_cmd"
            for cmd in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
                if command -v "$cmd" >/dev/null 2>&1 && check_python "$cmd"; then
                    PYTHON_BIN="$cmd"; break
                fi
            done
            [ -z "$PYTHON_BIN" ] && die "装完仍找不到合用的 python" "重开终端再跑一次安装器"
        else
            die "需要 Python 3.9+" "macOS: brew install python  或访问 python.org/downloads"
        fi
    else
        die "需要 Python 3.9+ 且本机没装包管理器" "访问 python.org/downloads 手动装"
    fi
fi

PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
ok "$PYTHON_BIN ($PY_VER)"

# ---------- [3/6] pip ----------
step 3 "检测 pip"
if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    fail "pip 不可用"
    if ask "用 ensurepip 引导 pip？" y; then
        "$PYTHON_BIN" -m ensurepip --upgrade --user 2>/dev/null || \
            die "ensurepip 失败" "试试 $PKG_INSTALL python3-pip"
    else
        die "需要 pip" "$PKG_INSTALL python3-pip"
    fi
fi

PIP_VER="$("$PYTHON_BIN" -m pip --version 2>/dev/null | awk '{print $2}')"
# pip 21.3+ 才能装 pyproject-only editable；我们的 connector 是 pyproject 的
PIP_OK="$("$PYTHON_BIN" -c "
import sys
try:
    from packaging.version import Version
    print(int(Version('$PIP_VER') >= Version('21.3')))
except Exception:
    parts = '$PIP_VER'.split('.')
    major = int(parts[0]) if parts and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    print(int(major > 21 or (major == 21 and minor >= 3)))
" 2>/dev/null || echo 0)"
if [ "$PIP_OK" != "1" ]; then
    warn "pip $PIP_VER 太老（需要 ≥ 21.3）"
    if ask "升级 pip？" y; then
        "$PYTHON_BIN" -m pip install --user --upgrade pip >/dev/null 2>&1 || \
            die "升级 pip 失败"
    fi
fi
PIP_VER="$("$PYTHON_BIN" -m pip --version 2>/dev/null | awk '{print $2}')"
ok "pip $PIP_VER"

# ---------- [4/6] pipx（首选）或退化到 venv ----------
step 4 "检测 pipx（隔离安装环境）"

INSTALL_MODE=""
if "$PYTHON_BIN" -m pipx --version >/dev/null 2>&1; then
    INSTALL_MODE="pipx"
    ok "pipx 已装"
elif command -v pipx >/dev/null 2>&1; then
    INSTALL_MODE="pipx"
    ok "pipx 已装（系统）"
else
    fail "未找到 pipx"
    if ask "从 PyPI 装 pipx？" y; then
        "$PYTHON_BIN" -m pip install --user pipx >/dev/null 2>&1 || \
            warn "pipx 装失败 —— 退化到 venv 模式"
        if "$PYTHON_BIN" -m pipx --version >/dev/null 2>&1; then
            INSTALL_MODE="pipx"
            ok "pipx 装好"
        else
            INSTALL_MODE="venv"
            warn "pipx 不可用，将用独立 venv 装（~/.lobster-town-venv）"
        fi
    else
        INSTALL_MODE="venv"
        ok "退化到 venv 模式"
    fi
fi

# ---------- [5/6] 装 lobster-town ----------
step 5 "安装 lobster-town"

PKG_SPEC="lobster-town@git+https://github.com/JuneLiu1999/lobster-town.git#subdirectory=connector"
WRAPPER_DIR="$HOME/.local/bin"
mkdir -p "$WRAPPER_DIR"

if [ "$INSTALL_MODE" = "pipx" ]; then
    printf '      %b⠼%b 从 GitHub 拉源码 + 装到 pipx 环境...\n' "$C_DIM" "$C_RESET"
    # pipx install 已经会处理 wrapper 放进 ~/.local/bin
    if "$PYTHON_BIN" -m pipx install --force "$PKG_SPEC" >/tmp/lobster-install.log 2>&1; then
        ok "通过 pipx 装好"
    else
        warn "pipx 装失败，退化到 venv"
        cat /tmp/lobster-install.log >&2
        INSTALL_MODE="venv"
    fi
fi

if [ "$INSTALL_MODE" = "venv" ]; then
    VENV_DIR="$HOME/.lobster-town-venv"
    printf '      %b⠼%b 创建 venv：%s\n' "$C_DIM" "$C_RESET" "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" >/dev/null 2>&1 || die "创建 venv 失败"
    "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1
    printf '      %b⠼%b 从 GitHub 拉源码 + 装入 venv...\n' "$C_DIM" "$C_RESET"
    "$VENV_DIR/bin/pip" install "$PKG_SPEC" >/tmp/lobster-install.log 2>&1 || {
        cat /tmp/lobster-install.log >&2
        die "安装失败" "看上面 pip 报错"
    }
    # symlink wrapper
    ln -sf "$VENV_DIR/bin/lobster-town" "$WRAPPER_DIR/lobster-town"
    ok "通过 venv 装好（symlink → $WRAPPER_DIR/lobster-town）"
fi

LT_VER="$("$WRAPPER_DIR/lobster-town" --version 2>/dev/null | awk '{print $NF}' || echo "?")"
ok "lobster-town $LT_VER"

# ---------- [6/6] PATH ----------
step 6 "检测 PATH"

if echo ":$PATH:" | grep -q ":$WRAPPER_DIR:"; then
    ok "$WRAPPER_DIR 已在 PATH"
    PATH_OK=1
else
    fail "$WRAPPER_DIR 不在 PATH"
    PATH_OK=0
    # 找 user 的 shell rc
    case "${SHELL:-}" in
        */zsh)  RC_FILE="$HOME/.zshrc" ;;
        */bash) RC_FILE="$HOME/.bashrc" ;;
        */fish) RC_FILE="$HOME/.config/fish/config.fish" ;;
        *) RC_FILE="$HOME/.profile" ;;
    esac
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    [ "${SHELL##*/}" = "fish" ] && PATH_LINE='set -gx PATH $HOME/.local/bin $PATH'
    # 注意：bash 3.2 / 一些 sh 实现里 `$VAR？`（非 ASCII 紧贴变量名）会被
    # 错误解析成 `${VAR?...}`（"未设报错"语法），触发 unbound 报错。
    # 强制用 ${VAR} 大括号形式分隔，万无一失。
    if ask "把 \"${PATH_LINE}\" 追加到 ${RC_FILE} ？" y; then
        printf '\n# Added by lobster-town installer\n%s\n' "${PATH_LINE}" >> "${RC_FILE}"
        ok "已追加到 ${RC_FILE}"
        PATH_OK=2  # 需要 source 才生效
    fi
fi

# ---------- 完成 ----------
p ""
p "${C_GRN}╔═══════════════════════════════════════════════════════╗${C_RESET}"
p "${C_GRN}║${C_RESET}                                                       ${C_GRN}║${C_RESET}"
p "${C_GRN}║${C_RESET}   ${C_BOLD}✅ 安装完成！${C_RESET}                                       ${C_GRN}║${C_RESET}"
p "${C_GRN}║${C_RESET}                                                       ${C_GRN}║${C_RESET}"
case "$PATH_OK" in
    1) # 已在 PATH
        p "${C_GRN}║${C_RESET}   ${C_BOLD}下一步${C_RESET}                                              ${C_GRN}║${C_RESET}"
        p "${C_GRN}║${C_RESET}     ${C_CYAN}lobster-town start${C_RESET}                                ${C_GRN}║${C_RESET}"
        ;;
    2) # 加了 rc 但还没生效
        p "${C_GRN}║${C_RESET}   ${C_BOLD}下一步${C_RESET}                                              ${C_GRN}║${C_RESET}"
        p "${C_GRN}║${C_RESET}     1. 重开终端 ${C_DIM}（或 source ~/.zshrc）${C_RESET}                ${C_GRN}║${C_RESET}"
        p "${C_GRN}║${C_RESET}     2. ${C_CYAN}lobster-town start${C_RESET}                             ${C_GRN}║${C_RESET}"
        ;;
    0) # 没加 rc
        p "${C_GRN}║${C_RESET}   ${C_BOLD}下一步${C_RESET}                                              ${C_GRN}║${C_RESET}"
        p "${C_GRN}║${C_RESET}     ${C_CYAN}~/.local/bin/lobster-town start${C_RESET}                   ${C_GRN}║${C_RESET}"
        p "${C_GRN}║${C_RESET}     ${C_DIM}（或自己把 ~/.local/bin 加进 PATH）${C_RESET}                ${C_GRN}║${C_RESET}"
        ;;
esac
p "${C_GRN}║${C_RESET}                                                       ${C_GRN}║${C_RESET}"
p "${C_GRN}║${C_RESET}   ${C_DIM}找内测组织者 JY 拿邀请码${C_RESET}                            ${C_GRN}║${C_RESET}"
p "${C_GRN}║${C_RESET}                                                       ${C_GRN}║${C_RESET}"
p "${C_GRN}╚═══════════════════════════════════════════════════════╝${C_RESET}"
p ""
p "${C_DIM}🐛 卡住了？lobster-town status 看健康度${C_RESET}"
p "${C_DIM}📚 详细文档: github.com/JuneLiu1999/lobster-town${C_RESET}"
p ""

# 检测 OpenClaw（不强制；warn 提示就好）
if ! command -v openclaw >/dev/null 2>&1; then
    p "${C_YEL}💡 提示：本机没装 OpenClaw，龙虾不会自主行动。${C_RESET}"
    p "${C_YEL}   想要它"活"过来？装 OpenClaw 后再 lobster-town start 即可。${C_RESET}"
    p ""
fi
