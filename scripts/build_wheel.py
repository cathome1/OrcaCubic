#!/usr/bin/env python3
"""Build a PEP 427 pure-Python wheel (.whl) of OrcaCubic for OrcaSlicer 2.5+ Plugin Hub.

Uses only Python stdlib (no external build/setuptools dependencies).
"""

from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = ROOT / "dist"
VERSION = "0.3.5"
DIST_NAME = "orcacubic"
WHEEL_FILENAME = f"{DIST_NAME}-{VERSION}-py3-none-any.whl"
DIST_INFO = f"{DIST_NAME}-{VERSION}.dist-info"


def _record_entry(arcname: str, data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    b64 = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{arcname},sha256={b64},{len(data)}"


def build_wheel() -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIST_DIR / WHEEL_FILENAME

    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {DIST_NAME}\n"
        f"Version: {VERSION}\n"
        "Summary: Native Anycubic Cloud plugin for OrcaSlicer 2.5+ — remote printing, live telemetry, WebRTC camera & timelapses\n"
        "Home-page: https://github.com/cathome1/OrcaCubic\n"
        "Author: cathome1\n"
        "License: MIT\n"
        "Requires-Python: >=3.9\n"
        "Description-Content-Type: text/markdown\n"
        "\n"
        f"{readme_text}\n"
    ).encode("utf-8")

    wheel_meta = (
        "Wheel-Version: 1.0\n"
        "Generator: orcacubic-build-wheel\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("utf-8")

    top_level = b"orcacubic\n"

    entries: list[tuple[str, bytes]] = []
    for py_file in sorted((ROOT / "orcacubic").glob("*.py")):
        entries.append((f"orcacubic/{py_file.name}", py_file.read_bytes()))

    # Bundle the OrcaSlicer plugin module as orcacubic/plugin.py
    plugin_src = (ROOT / "plugin" / "orcacubic_plugin.py").read_bytes()
    entries.append(("orcacubic/plugin.py", plugin_src))

    entries.append((f"{DIST_INFO}/METADATA", metadata))
    entries.append((f"{DIST_INFO}/WHEEL", wheel_meta))
    entries.append((f"{DIST_INFO}/top_level.txt", top_level))
    entries.append((f"{DIST_INFO}/LICENSE", license_text.encode("utf-8")))

    records = [_record_entry(name, data) for name, data in entries]
    record_path = f"{DIST_INFO}/RECORD"
    records.append(f"{record_path},,")
    record_bytes = ("\n".join(records) + "\n").encode("utf-8")

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
        zf.writestr(record_path, record_bytes)

    return out_path


if __name__ == "__main__":
    whl = build_wheel()
    print(f"Built wheel: {whl} ({whl.stat().st_size} bytes)")
