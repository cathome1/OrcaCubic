"""Cross-platform launcher for the Anycubic Cloud login window and OS folder picker.

- macOS: uses the native Swift WKWebView helper (`anycubic-login-helper`),
  with automatic fallback to Chromium/Edge/Chrome CDP app mode.
- Windows / Linux: opens a clean standalone browser window (Microsoft Edge /
  Google Chrome / Chromium) via `--app=https://cloud-universe.anycubic.com/`
  and reads `localStorage['XX-Token']` over the local Chrome DevTools Protocol
  (pure Python stdlib, zero external dependencies).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .auth import AnycubicAuthError

ANYCUBIC_LOGIN_URL = "https://cloud-universe.anycubic.com/"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def find_login_helper(extra_search_dirs: Optional[list[Path]] = None) -> Optional[Path]:
    """Locate compiled macOS anycubic-login-helper binary."""
    if platform.system() != "Darwin":
        return None

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


def find_chromium_browser() -> Optional[Path]:
    """Locate Microsoft Edge, Google Chrome, Chromium, or Brave on Windows/Linux/macOS."""
    system = platform.system()
    candidates: list[Path] = []

    if system == "Windows":
        env_roots = [
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("LOCALAPPDATA"),
        ]
        rel_paths = [
            Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
            Path("Google") / "Chrome" / "Application" / "chrome.exe",
            Path("BraveSoftware") / "Brave-Browser" / "Application" / "brave.exe",
            Path("Chromium") / "Application" / "chrome.exe",
        ]
        for root in env_roots:
            if not root:
                continue
            base = Path(root)
            for rel in rel_paths:
                candidates.append(base / rel)
        for name in ("msedge", "msedge.exe", "chrome", "chrome.exe", "brave", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    elif system == "Linux":
        for name in (
            "microsoft-edge",
            "microsoft-edge-stable",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "brave-browser",
        ):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    elif system == "Darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            ]
        )

    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def get_required_process_grants(
    extra_search_dirs: Optional[list[Path]] = None,
) -> list[str]:
    """Return executable paths the plugin may spawn on this OS (for OrcaSlicer audit)."""
    grants: list[str] = []

    def _add(p: Optional[Path | str]) -> None:
        if not p:
            return
        s = str(p)
        if s not in grants:
            grants.append(s)

    _add(find_login_helper(extra_search_dirs=extra_search_dirs))
    _add(find_chromium_browser())

    system = platform.system()
    if system == "Windows":
        _add(shutil.which("powershell"))
        _add(shutil.which("powershell.exe"))
    elif system == "Linux":
        _add(shutil.which("zenity"))
        _add(shutil.which("kdialog"))

    return grants


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("WebSocket connection closed")
        buf.extend(chunk)
    return bytes(buf)


def _ws_send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode("utf-8")
    mask_key = os.urandom(4)
    header = bytearray([0x81])  # FIN + text frame
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask_key)
    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _ws_recv_frame(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = _recv_exact(sock, 2)
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _recv_exact(sock, 8))
    mask_key = _recv_exact(sock, 4) if is_masked else b""
    data = _recv_exact(sock, length) if length else b""
    if is_masked:
        data = bytes(b ^ mask_key[i & 3] for i, b in enumerate(data))
    return opcode, data


def _cdp_eval_js(ws_url: str, expression: str, timeout: float = 3.0) -> Any:
    """Evaluate a JS expression on a Chrome DevTools Protocol page target via stdlib WS."""
    parsed = urllib.parse.urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode("ascii")

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(req)

        # Read HTTP upgrade response headers
        resp = bytearray()
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP handshake failed")
            resp.extend(chunk)
        header_bytes = bytes(resp.split(b"\r\n\r\n", 1)[0])
        status_line = header_bytes.split(b"\r\n", 1)[0].decode("latin-1", errors="ignore")
        if "101" not in status_line:
            raise ConnectionError(f"Unexpected CDP handshake status: {status_line}")

        expected_accept = base64.b64encode(
            hashlib.sha1((ws_key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept.encode("ascii") not in header_bytes:
            raise ConnectionError("Invalid Sec-WebSocket-Accept from CDP")

        cmd = json.dumps(
            {
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }
        )
        _ws_send_text(sock, cmd)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opcode, data = _ws_recv_frame(sock)
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping -> pong
                continue
            if opcode != 0x1:
                continue
            msg = json.loads(data.decode("utf-8", errors="replace"))
            if msg.get("id") == 1:
                return msg.get("result", {}).get("result", {}).get("value")
    return None


def _launch_macos_helper(
    helper: Path,
    reset_session: bool = False,
    timeout: int = 300,
) -> str:
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


def _launch_browser_cdp_login(
    browser_path: Path,
    reset_session: bool = False,
    timeout: int = 300,
    profile_dir: Optional[Path] = None,
) -> str:
    """Open Edge/Chrome in standalone --app mode and capture XX-Token via local CDP."""
    port = _pick_free_port()
    base_profile = profile_dir or (Path(tempfile.gettempdir()) / "orcacubic_login_profile")
    if reset_session:
        user_data_dir = base_profile.parent / f"{base_profile.name}_{int(time.time())}"
    else:
        user_data_dir = base_profile

    cmd = [
        str(browser_path),
        f"--app={ANYCUBIC_LOGIN_URL}",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--window-size=480,760",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise AnycubicAuthError(
            f"Не удалось открыть окно браузера ({browser_path.name}): {exc}"
        ) from exc

    deadline = time.monotonic() + timeout
    saw_cdp_ready = False
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AnycubicAuthError("Окно входа закрыто до завершения авторизации.")

            targets: list[dict[str, Any]] = []
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=1.5
                ) as resp:
                    targets = json.loads(resp.read().decode("utf-8", errors="replace"))
                    saw_cdp_ready = True
            except Exception:
                time.sleep(0.5)
                continue

            page_targets = [
                t
                for t in targets
                if isinstance(t, dict)
                and t.get("type") == "page"
                and t.get("webSocketDebuggerUrl")
            ]
            if saw_cdp_ready and not page_targets:
                raise AnycubicAuthError("Окно входа закрыто до завершения авторизации.")

            # Prefer Anycubic Cloud pages
            ordered = sorted(
                page_targets,
                key=lambda t: 0 if "anycubic.com" in str(t.get("url", "")).lower() else 1,
            )
            for target in ordered:
                ws_url = str(target.get("webSocketDebuggerUrl") or "")
                if not ws_url:
                    continue
                try:
                    val = _cdp_eval_js(
                        ws_url,
                        "window.localStorage ? (window.localStorage.getItem('XX-Token') || '') : ''",
                        timeout=2.0,
                    )
                except Exception:
                    continue
                if isinstance(val, str) and val.strip():
                    return val.strip()

            time.sleep(0.6)

        raise AnycubicAuthError("Время ожидания входа в Anycubic Cloud истекло.")
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def launch_login_window(
    reset_session: bool = False,
    timeout: int = 300,
    extra_search_dirs: Optional[list[Path]] = None,
    profile_dir: Optional[Path] = None,
) -> str:
    """Open Anycubic Cloud login window on macOS, Windows, or Linux and return XX-Token."""
    helper = find_login_helper(extra_search_dirs=extra_search_dirs)
    if helper is not None:
        return _launch_macos_helper(
            helper=helper,
            reset_session=reset_session,
            timeout=timeout,
        )

    browser = find_chromium_browser()
    if browser is not None:
        return _launch_browser_cdp_login(
            browser_path=browser,
            reset_session=reset_session,
            timeout=timeout,
            profile_dir=profile_dir,
        )

    raise AnycubicAuthError(
        "Не найдено окно входа (на macOS соберите anycubic-login-helper через scripts/install.py; "
        "на Windows/Linux установите Microsoft Edge или Google Chrome) — либо вставьте XX-Token вручную."
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
