"""
身份管理：生成和加载 Ed25519 密钥对，管理本地 Device ID。

存储位置（按平台）：
  - Linux / Mac: ~/.lobster-town/
  - Windows: %USERPROFILE%\\.lobster-town\\

文件：
  - device.json   : {"device_id": "agent-xxxx", "public_key_hex": "...", "server": "..."}
  - private_key.bin : 32 字节原始私钥（权限 0o600）
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import nacl.signing


CONFIG_DIR = Path(os.environ.get("LOBSTER_TOWN_HOME") or (Path.home() / ".lobster-town"))
DEVICE_FILE = CONFIG_DIR / "device.json"
PRIVATE_KEY_FILE = CONFIG_DIR / "private_key.bin"


@dataclass
class Identity:
    """一个 Connector 实例的身份。"""

    device_id: str
    signing_key: nacl.signing.SigningKey  # 用于签名挑战
    public_key_hex: str
    server_url: str

    @property
    def verify_key_bytes(self) -> bytes:
        return self.signing_key.verify_key.encode()

    def sign(self, message: bytes) -> bytes:
        """对消息原文做 Ed25519 签名，返回纯签名（64 字节）。"""
        return self.signing_key.sign(message).signature


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        # 权限 700（Windows 上会静默忽略）
        os.chmod(CONFIG_DIR, stat.S_IRWXU)
    except OSError:
        pass


def _compute_device_id(public_key_bytes: bytes) -> str:
    """Device ID = agent-<sha256(pub_key)[:8]>，和后端保持一致。"""
    h = hashlib.sha256(public_key_bytes).hexdigest()[:8]
    return f"agent-{h}"


def load_or_create_identity(server_url: str) -> tuple[Identity, bool]:
    """
    加载本地身份；若不存在则新建并保存。

    返回 (identity, is_new)。
    """
    _ensure_config_dir()

    if DEVICE_FILE.exists() and PRIVATE_KEY_FILE.exists():
        with DEVICE_FILE.open() as f:
            data = json.load(f)
        private_key_bytes = PRIVATE_KEY_FILE.read_bytes()
        if len(private_key_bytes) != 32:
            raise RuntimeError(f"Corrupted private key file ({PRIVATE_KEY_FILE})")

        signing_key = nacl.signing.SigningKey(private_key_bytes)
        identity = Identity(
            device_id=data["device_id"],
            signing_key=signing_key,
            public_key_hex=data["public_key_hex"],
            server_url=data.get("server", server_url),
        )
        return identity, False

    # 新建
    signing_key = nacl.signing.SigningKey.generate()
    public_key_bytes = signing_key.verify_key.encode()
    public_key_hex = public_key_bytes.hex()
    device_id = _compute_device_id(public_key_bytes)

    # 保存私钥（原始 32 字节）
    PRIVATE_KEY_FILE.write_bytes(signing_key.encode())
    try:
        os.chmod(PRIVATE_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    except OSError:
        pass

    # 保存 device.json
    with DEVICE_FILE.open("w") as f:
        json.dump(
            {
                "device_id": device_id,
                "public_key_hex": public_key_hex,
                "server": server_url,
            },
            f,
            indent=2,
        )

    identity = Identity(
        device_id=device_id,
        signing_key=signing_key,
        public_key_hex=public_key_hex,
        server_url=server_url,
    )
    return identity, True


def forget_identity() -> None:
    """删除本地身份（用户主动 reset 时用）。"""
    for f in (DEVICE_FILE, PRIVATE_KEY_FILE):
        if f.exists():
            f.unlink()
