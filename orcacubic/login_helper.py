"""Launcher for the native macOS Anycubic Cloud WKWebView login window."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .auth import AnycubicAuthError


def find_login_helper(extra_search_dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Locate compiled anycubic-login-helper binary."""
    candidates: list[Path] = []
    if extra_search_dirs:
        for base in extra_search_dirs:
            candidates.append(base / "anycubic-login-helper")
            candidates.append(base / "bin" / "anycubic-login-helper")

    pkg_root = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            pkg_root / "bin" / "anycubic-login-helper",
            pkg_root / "anycubic-login-helper",
        ]
    )

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def launch_login_window(
    reset_session: bool = False,
    timeout: int = 300,
    extra_search_dirs: Optional[list[Path]] = None,
) -> str:
    """Open native macOS login window and return captured XX-Token."""
    helper = find_login_helper(extra_search_dirs=extra_search_dirs)
    if helper is None:
        raise AnycubicAuthError(
            "Помощник авторизации (anycubic-login-helper) не найден. "
            "Запустите ./scripts/install_macos.sh для сборки или введите XX-Token вручную."
        )

    cmd = [str(helper)]
    if reset_session:
        cmd.append("--reset-session")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnycubicAuthError(
            "Время ожидания входа в Anycubic Cloud истекло."
        ) from exc
    except OSError as exc:
        raise AnycubicAuthError(
            f"Не удалось запустить окно авторизации: {exc}"
        ) from exc

    for line in (proc.stdout or "").splitlines():
        if line.startswith("TOKEN:"):
            token = line[len("TOKEN:") :].strip()
            if token:
                return token

    if proc.returncode == 2:
        raise AnycubicAuthError("Окно входа закрыто до завершения авторизации.")

    raise AnycubicAuthError(
        "Не удалось получить XX-Token из окна авторизации. Попробуйте ещё раз "
        "или вставьте XX-Token вручную."
    )


def pick_folder_dialog(
    extra_search_dirs: Optional[list[Path]] = None,
) -> Optional[str]:
    """Open native OS folder picker (Finder / Explorer / Zenity) and return selected path."""
    system = platform.system()
    if system == "Darwin":
        helper = find_login_helper(extra_search_dirs=extra_search_dirs)
        if helper is not None:
            proc = subprocess.run(
                [str(helper), "--choose-folder"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                if line.startswith("DIR:"):
                    return line[4:].strip()
            return None
        return None

    if system == "Windows":
        ps_cmd = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        out = (proc.stdout or "").strip()
        return out or None

    # Linux (zenity or kdialog)
    if shutil.which("zenity"):
        proc = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Папка для таймлапсов"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        out = (proc.stdout or "").strip()
        return out or None
    if shutil.which("kdialog"):
        proc = subprocess.run(
            ["kdialog", "--getexistingdirectory"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        out = (proc.stdout or "").strip()
        return out or None
    return None
