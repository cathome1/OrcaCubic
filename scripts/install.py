#!/usr/bin/env python3
"""Cross-platform installer/updater for OrcaCubic plugin (macOS, Windows, Linux)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def get_orca_plugins_dir() -> Path:
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "OrcaSlicer" / "orca_plugins"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "OrcaSlicer" / "orca_plugins"
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return base / "OrcaSlicer" / "orca_plugins"


def maybe_build_macos_login_helper(repo_dir: Path, dest_bin_dir: Path) -> None:
    if platform.system() != "Darwin":
        return
    swiftc = shutil.which("swiftc")
    swift_src = repo_dir / "macos_login" / "AnycubicLoginHelper.swift"
    if not swiftc or not swift_src.is_file():
        print("==> swiftc не найден — нативное окно входа macOS пропущено (можно вводить XX-Token вручную).")
        return

    print("==> Сборка нативного окна входа macOS (WKWebView)...")
    repo_bin = repo_dir / "bin"
    repo_bin.mkdir(parents=True, exist_ok=True)
    helper_bin = repo_bin / "anycubic-login-helper"
    subprocess.run(
        [
            swiftc,
            "-O",
            "-framework",
            "Cocoa",
            "-framework",
            "WebKit",
            str(swift_src),
            "-o",
            str(helper_bin),
        ],
        check=True,
    )
    os.chmod(helper_bin, 0o755)
    dest_bin_dir.mkdir(parents=True, exist_ok=True)
    dest_helper = dest_bin_dir / "anycubic-login-helper"
    shutil.copy2(helper_bin, dest_helper)
    os.chmod(dest_helper, 0o755)
    print(f"    Собран и установлен: {dest_helper}")


def main() -> int:
    repo_dir = Path(__file__).resolve().parent.parent
    plugin_dest = get_orca_plugins_dir() / "orcacubic"
    data_dir = plugin_dest / "data"
    config_file = data_dir / "config.json"

    print(f"==> Установка плагина OrcaCubic ({platform.system()})...")
    plugin_dest.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "timelapses").mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass

    if not config_file.exists():
        config_file.write_text("{}", encoding="utf-8")
        try:
            os.chmod(config_file, 0o600)
        except OSError:
            pass

    tmp_gcode = data_dir / "_tmp_upload.gcode"
    if not tmp_gcode.exists():
        tmp_gcode.write_bytes(b"")
        try:
            os.chmod(tmp_gcode, 0o600)
        except OSError:
            pass

    shutil.copy2(repo_dir / "plugin" / "orcacubic_plugin.py", plugin_dest / "orcacubic_plugin.py")

    dest_pkg = plugin_dest / "orcacubic"
    if dest_pkg.exists():
        shutil.rmtree(dest_pkg)
    shutil.copytree(
        repo_dir / "orcacubic",
        dest_pkg,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    maybe_build_macos_login_helper(repo_dir, plugin_dest / "bin")

    print("")
    print(f"Готово! Плагин установлен в:\n  {plugin_dest}")
    print("")
    print("Дальше в OrcaSlicer 2.5+:")
    print("  1. Перезапустите OrcaSlicer (или включите плагин в меню Плагины).")
    print("  2. Preferences -> Developer -> Experimental -> включите 'Use printer agents instead of print hosts'.")
    print("  3. В настройках принтера Kobra 2 Pro -> Basic information -> Advanced -> Printer Agent выберите 'Anycubic Cloud'.")
    print("  4. Откройте вкладку 'OrcaCubic' и выполните вход.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
