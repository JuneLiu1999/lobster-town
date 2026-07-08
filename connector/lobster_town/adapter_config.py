"""
Adapter 配置管理 —— 读写 ~/.lobster-town/config.json。

支持的配置项：
  adapter       : "openclaw" | "direct"（默认 openclaw）
  llm_base_url  : OpenAI 兼容 API 地址
  llm_api_key   : API 密钥（落盘 chmod 600）
  llm_model     : 模型名
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from lobster_town.identity import CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS: dict[str, Any] = {
    "adapter": "openclaw",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
}

# CLI key（带连字符）→ JSON key（下划线）的映射
_KEY_ALIASES: dict[str, str] = {
    "llm-base-url": "llm_base_url",
    "llm-api-key": "llm_api_key",
    "llm-model": "llm_model",
}


def _normalize_key(key: str) -> str:
    return _KEY_ALIASES.get(key, key)


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return dict(DEFAULTS)
    try:
        with CONFIG_FILE.open() as f:
            data = json.load(f)
        merged = dict(DEFAULTS)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def get_value(key: str) -> Any:
    key = _normalize_key(key)
    return load_config().get(key)


def set_value(key: str, value: str) -> None:
    key = _normalize_key(key)
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def mask_key(api_key: str) -> str:
    if not api_key or len(api_key) < 8:
        return "***"
    return api_key[:4] + "..." + api_key[-4:]
