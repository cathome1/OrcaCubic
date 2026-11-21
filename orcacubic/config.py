"""Local configuration storage and Anycubic Slicer Next fallback reader."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

def _default_config_path() -> Path:
    override = os.environ.get("ORCACUBIC_CONFIG_PATH", "").strip()
    if override:
        return Path(override)
    pkg_parent = Path(__file__).resolve().parent.parent
    if "orca_plugins" in pkg_parent.parts:
        return pkg_parent / "data" / "config.json"
    return Path.home() / ".config" / "orcacubic" / "config.json"


DEFAULT_CONFIG_PATH = _default_config_path()
DEFAULT_CONFIG_DIR = DEFAULT_CONFIG_PATH.parent
ANYCUBIC_SLICER_NEXT_CONF = (
    Path.home()
    / "Library"
    / "Application Support"
    / "AnycubicSlicerNext"
    / "AnycubicSlicerNext.conf"
)


@dataclass
class OrcaCubicConfig:
    token: str = ""
    printer_id: Optional[int] = None
    printer_name: str = ""
    default_mode: str = "print"  # "print" | "upload_only"
    camera_transforms: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    timelapse_dir: str = ""
    timelapse_auto: bool = False

    def sanitized_dict(self) -> dict:
        """Return config dict safe for UI/logging (never exposes full token)."""
        return {
            "has_token": bool(self.token),
            "token_preview": mask_token(self.token),
            "printer_id": self.printer_id,
            "printer_name": self.printer_name,
            "default_mode": self.default_mode,
            "camera_transforms": self.camera_transforms,
            "timelapse_dir": self.timelapse_dir,
            "timelapse_auto": self.timelapse_auto,
        }


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> OrcaCubicConfig:
    """Load saved config; environment variable ANYCUBIC_TOKEN overrides saved token."""
    cfg = OrcaCubicConfig()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg.token = str(raw.get("token", "") or "").strip()
            pid = raw.get("printer_id")
            cfg.printer_id = int(pid) if pid is not None else None
            cfg.printer_name = str(raw.get("printer_name", "") or "")
            mode = str(raw.get("default_mode", "print") or "print")
            cfg.default_mode = mode if mode in ("print", "upload_only") else "print"
            ct = raw.get("camera_transforms")
            if isinstance(ct, dict):
                cfg.camera_transforms = ct
            cfg.timelapse_dir = str(raw.get("timelapse_dir", "") or "").strip()
            cfg.timelapse_auto = bool(raw.get("timelapse_auto", False))
        except (OSError, ValueError, TypeError):
            pass

    env_token = os.environ.get("ANYCUBIC_TOKEN", "").strip()
    if env_token:
        cfg.token = env_token

    return cfg


def save_config(cfg: OrcaCubicConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist config with restrictive file permissions (0600)."""
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), ensure_ascii=False, indent=2)
    path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def try_read_slicer_next_token(
    conf_path: Path = ANYCUBIC_SLICER_NEXT_CONF,
) -> Tuple[Optional[str], str]:
    """Best-effort reader for legacy unencrypted AnycubicSlicerNext.conf.

    Returns (token_or_none, status_message).
    """
    if not conf_path.exists():
        return None, "Конфиг Anycubic Slicer Next не найден."

    try:
        data = json.loads(conf_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "Не удалось прочитать AnycubicSlicerNext.conf."

    cloud = data.get("anycubic_cloud")
    if not isinstance(cloud, dict):
        return None, "Секция anycubic_cloud отсутствует в AnycubicSlicerNext.conf."

    plain_token = cloud.get("access_token") or cloud.get("token")
    if isinstance(plain_token, str) and plain_token.strip():
        return plain_token.strip(), "Токен найден в AnycubicSlicerNext.conf."

    # Modern Anycubic Slicer Next encrypts keys/values with AES (base64 keys ending in '==')
    if any(isinstance(k, str) and k.endswith("==") for k in cloud.keys()):
        return (
            None,
            "Anycubic Slicer Next шифрует токен в AnycubicSlicerNext.conf. "
            "Используйте кнопку «Войти в Anycubic Cloud» или вставьте XX-Token вручную.",
        )

    return None, "Токен в AnycubicSlicerNext.conf не найден."
