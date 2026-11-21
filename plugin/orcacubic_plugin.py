# /// script
# requires-python = ">=3.9"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "orcacubic"
# name = "OrcaCubic (Anycubic Cloud)"
# description = "Remote printing, live dashboard, WebRTC camera & timelapses for Anycubic Cloud FDM printers"
# author = "cathome1"
# version = "0.3.5"
# ///

"""OrcaSlicer 2.5+ plugin for Anycubic Cloud (all Anycubic Cloud FDM printers)."""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import tempfile
import threading
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure sibling `orcacubic` package is importable and freshly reloaded on local plugin toggle
_THIS_FILE = Path(__file__).resolve()
_PLUGIN_DIR = _THIS_FILE.parent


def _resolve_plugin_root(path: Path) -> Path:
    """Return persistent plugin root (outside __whl_extracted__ for cloud .whl installs)."""
    for parent in path.parents:
        if parent.name == "__whl_extracted__":
            return parent.parent
    return path.parent


_PLUGIN_ROOT = _resolve_plugin_root(_THIS_FILE)

if __name__ != "orcacubic.plugin":
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    if str(_PLUGIN_DIR.parent) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR.parent))

    for _mod_name in list(sys.modules.keys()):
        if _mod_name == "orcacubic" or _mod_name.startswith("orcacubic."):
            sys.modules.pop(_mod_name, None)

import orca  # type: ignore[import-not-found]

from orcacubic import __version__
from orcacubic.auth import AnycubicAuthError, AnycubicError, AnycubicTokenExpiredError
from orcacubic.camera import close_camera_session, open_camera_session
from orcacubic.client import AnycubicCloudClient, PrinterSummary
from orcacubic.config import (
    OrcaCubicConfig,
    load_config,
    save_config,
)
from orcacubic.gcode_adapter import adapt_gcode_stream
from orcacubic.login_helper import (
    find_chromium_browser,
    find_login_helper,
    get_required_process_grants,
    launch_login_window,
    pick_folder_dialog,
)
from orcacubic.mqtt_client import AnycubicMqttWatcher

_DATA_DIR = _PLUGIN_ROOT / "data"
_CONFIG_FILE = _DATA_DIR / "config.json"
_TIMELAPSE_DIR = _DATA_DIR / "timelapses"

AGENT_ID = "anycubic_cloud"
AGENT_NAME = "Anycubic Cloud"

# OrcaSlicer PrinterAgent return codes
ORCA_SUCCESS = 0
ORCA_ERROR_GENERIC = -1
ORCA_ERROR_AUTH = -7001
ORCA_ERROR_NO_PRINTER = -7002
ORCA_ERROR_CANCELLED = -7003


def _resolve_target_printer(
    client: AnycubicCloudClient, cfg: OrcaCubicConfig
) -> PrinterSummary:
    printers = client.get_printers()
    if not printers:
        raise AnycubicError("В аккаунте Anycubic Cloud не найдено ни одного принтера.")

    if cfg.printer_id is not None:
        for p in printers:
            if p.printer_id == cfg.printer_id:
                return p

    # Auto-prefer the first online printer if user hasn't picked one yet
    for p in printers:
        if p.online:
            return p

    return printers[0]


# Shared references so the Pages tab and PrinterAgent stay in sync
_ACTIVE_AGENT: Optional["AnycubicPrinterAgent"] = None
_ACTIVE_PAGE: Optional["AnycubicCloudPage"] = None


def _extract_gcode_path(params: Any) -> Tuple[Path, Optional[Path]]:
    """Return (gcode_path, temp_path_to_cleanup). Extracts .gcode from .3mf and adapts metadata."""
    src_path: Optional[Path] = None
    for attr in ("filename", "dst_file", "ftp_file"):
        val = getattr(params, attr, "")
        if isinstance(val, str) and val.strip():
            p = Path(val.strip())
            if p.is_file():
                src_path = p
                break
    if src_path is None:
        raise AnycubicError(
            "OrcaSlicer не передал путь к нарезанному .gcode файлу (PrintParams.filename)."
        )

    model_name = (
        getattr(params, "task_name", "")
        or getattr(params, "project_name", "")
        or src_path.stem
    )
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _DATA_DIR / "_tmp_upload.gcode"
    if src_path.suffix.lower() == ".3mf" and zipfile.is_zipfile(src_path):
        with zipfile.ZipFile(src_path, "r") as zf:
            gcode_entries = [
                n for n in zf.namelist() if n.lower().endswith(".gcode")
            ]
            if not gcode_entries:
                raise AnycubicError(
                    f"Архив {src_path.name} не содержит .gcode внутри."
                )
            chosen = next(
                (n for n in gcode_entries if "plate_1" in n.lower()),
                gcode_entries[0],
            )
            with zf.open(chosen, "r") as src_f:
                adapt_gcode_stream(src_f, tmp_path, model_name=str(model_name))
            return tmp_path, tmp_path

    with src_path.open("rb") as src_f:
        adapt_gcode_stream(src_f, tmp_path, model_name=str(model_name))
    return tmp_path, tmp_path


def _extract_remote_name(params: Any, gcode_path: Path) -> str:
    for attr in ("task_name", "project_name", "dst_file"):
        val = getattr(params, attr, "")
        if isinstance(val, str) and val.strip():
            name = Path(val.strip()).name
            if name:
                if name.lower().endswith(".gcode.3mf"):
                    return name[:-4]
                if name.lower().endswith(".3mf"):
                    return f"{name[:-4]}.gcode"
                return name if name.lower().endswith(".gcode") else f"{name}.gcode"
    return gcode_path.name


class AnycubicPrinterAgent(orca.printer_agent.PrinterAgentBase):
    """Native OrcaSlicer PrinterConnection agent backed by Anycubic Cloud."""

    def __init__(self) -> None:
        global _ACTIVE_AGENT
        super().__init__()
        self._selected_dev_id: str = ""
        self._callbacks: Dict[str, Any] = {}
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._summaries: Dict[int, PrinterSummary] = {}
        _ACTIVE_AGENT = self

    def get_name(self) -> str:
        return AGENT_NAME

    def get_agent_info(self) -> Any:
        return orca.printer_agent.AgentInfo(
            id=AGENT_ID,
            name=AGENT_NAME,
            version=__version__,
            description="Remote printing, live telemetry & camera for Anycubic Cloud FDM printers (Kobra 2 / 3 / S1 / X)",
        )

    def _emit_ssdp_printer(
        self, printer_id: int, name: str, model: str = "", multi: bool = False
    ) -> None:
        ssdp_fn = self._callbacks.get("ssdp")
        if not callable(ssdp_fn):
            return
        display_name = f"ACCloud - {name}" if name else "ACCloud"
        payload = json.dumps(
            {
                "dev_name": display_name,
                "dev_id": str(printer_id),
                "dev_ip": "https://cloud-universe.anycubic.com",
                "dev_type": "",
                "dev_signal": "-50",
                "connect_type": "lan",
                "bind_state": "free",
                "sec_link": "",
                "ssdp_version": "",
                "connection_name": AGENT_NAME,
            }
        )
        try:
            ssdp_fn(payload)
        except Exception:
            pass

    def announce_printers(self, printers: Optional[List[PrinterSummary]] = None) -> None:
        """Push known Anycubic Cloud printers into OrcaSlicer's DeviceManager."""
        if printers:
            is_multi = len(printers) > 1
            for p in printers:
                self._emit_ssdp_printer(p.printer_id, p.name, p.model, multi=is_multi)
            return

        cfg = load_config(path=_CONFIG_FILE)
        if cfg.printer_id is not None:
            self._emit_ssdp_printer(
                cfg.printer_id,
                cfg.printer_name or "Anycubic Printer",
                "Anycubic Printer",
                multi=False,
            )

        if not cfg.token:
            return

        def _fetch_and_announce() -> None:
            try:
                client = AnycubicCloudClient(token=cfg.token)
                found = client.get_printers()
                is_multi = len(found) > 1
                for p in found:
                    self._emit_ssdp_printer(p.printer_id, p.name, p.model, multi=is_multi)
            except Exception:
                pass

        threading.Thread(target=_fetch_and_announce, daemon=True).start()

    def _emit_local_message(self, dev_id: str, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload)
        for key in ("local_message", "message"):
            fn = self._callbacks.get(key)
            if callable(fn):
                try:
                    fn(str(dev_id), raw)
                    return
                except Exception:
                    pass

    def _poll_live_summary(self, dev_id: str) -> Optional[PrinterSummary]:
        cfg = load_config(path=_CONFIG_FILE)
        pid_str = str(dev_id or self._selected_dev_id or (cfg.printer_id or ""))
        if not pid_str.isdigit():
            return None
        pid = int(pid_str)
        if not cfg.token:
            return self._summaries.get(pid)
        try:
            client = AnycubicCloudClient(token=cfg.token)
            summary = client.get_printer_status(pid)
            self._summaries[pid] = summary
            return summary
        except Exception:
            return self._summaries.get(pid)

    def _emit_push_status(
        self,
        dev_id: str,
        sequence_id: str = "1",
        summary: Optional[PrinterSummary] = None,
    ) -> None:
        did = str(dev_id or self._selected_dev_id or "0")
        cached = self._summaries.get(int(did)) if did.isdigit() else None
        st = summary or cached
        gcode_state = "IDLE"
        nozzle_temp = 25.0
        target_nozzle = 0.0
        bed_temp = 25.0
        target_bed = 0.0
        percent = 0
        remain_min = 0
        layer_num = 0
        total_layers = 0
        subtask_name = ""

        if st is not None:
            if st.status == "downloading":
                gcode_state = "PREPARE"
            elif st.status == "printing":
                gcode_state = "RUNNING"
            elif st.status == "paused":
                gcode_state = "PAUSE"
            else:
                gcode_state = "IDLE"
            if st.nozzle_temp is not None:
                nozzle_temp = st.nozzle_temp
            if st.target_nozzle_temp is not None:
                target_nozzle = st.target_nozzle_temp
            if st.bed_temp is not None:
                bed_temp = st.bed_temp
            if st.target_bed_temp is not None:
                target_bed = st.target_bed_temp
            percent = st.progress
            remain_min = st.remaining_time_min
            layer_num = st.current_layer
            total_layers = st.total_layers
            subtask_name = st.current_file

        self._emit_local_message(
            did,
            {
                "print": {
                    "command": "push_status",
                    "sequence_id": str(sequence_id),
                    "msg": 0,
                    "gcode_state": gcode_state,
                    "sdcard": True,
                    "home_flag": 256,
                    "nozzle_diameter": "0.4",
                    "nozzle_temper": nozzle_temp,
                    "nozzle_target_temper": target_nozzle,
                    "bed_temper": bed_temp,
                    "bed_target_temper": target_bed,
                    "mc_percent": percent,
                    "mc_remaining_time": remain_min,
                    "layer_num": layer_num,
                    "total_layer_num": total_layers,
                    "wifi_signal": "-50dBm",
                    "subtask_name": subtask_name,
                }
            },
        )

    def _start_heartbeat(self, dev_id: str) -> None:
        self._stop_heartbeat()
        self._heartbeat_stop = threading.Event()
        stop_ev = self._heartbeat_stop

        def _loop() -> None:
            seq = 2
            while not stop_ev.wait(10.0):
                summary = self._poll_live_summary(dev_id)
                self._emit_push_status(dev_id, sequence_id=str(seq), summary=summary)
                seq += 1

        self._heartbeat_thread = threading.Thread(target=_loop, daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()

    def _emit_printer_ready(self, dev_id: str, sequence_id: str = "1") -> None:
        """Send version + full push_status so OrcaSlicer marks the printer connected & ready."""
        did = str(dev_id or self._selected_dev_id or "0")
        cached = self._summaries.get(int(did)) if did.isdigit() else None
        fw_ver = (cached.firmware_version if cached and cached.firmware_version else "01.05.00.00")
        self._emit_local_message(
            did,
            {
                "info": {
                    "command": "get_version",
                    "sequence_id": str(sequence_id),
                    "result": "SUCCESS",
                    "module": [
                        {
                            "name": "ota",
                            "sw_ver": fw_ver,
                            "hw_ver": "1.0",
                            "sn": did,
                        },
                        {
                            "name": "mc",
                            "sw_ver": fw_ver,
                            "hw_ver": "1.0",
                            "sn": did,
                        },
                    ],
                }
            },
        )
        self._emit_local_message(
            did,
            {
                "info": {
                    "command": "get_capabilities",
                    "capabilities": {
                        "flags": {
                            "support_send_to_sd": True,
                            "support_timelapse": False,
                            "support_flow_calibration": False,
                        }
                    },
                }
            },
        )
        self._emit_push_status(did, sequence_id=sequence_id)

    # Required PrinterAgentBase lifecycle & callback hooks (pure virtual in C++)
    def connect_printer(self, params: Any) -> int:
        dev_id = str(getattr(params, "dev_id", "") or self._selected_dev_id or "")
        if dev_id:
            self._selected_dev_id = dev_id

        def _notify_connected() -> None:
            for key in ("local_connect", "connected"):
                fn = self._callbacks.get(key)
                if callable(fn):
                    try:
                        fn(0)
                    except TypeError:
                        try:
                            fn(dev_id, 0)
                        except Exception:
                            pass
                    except Exception:
                        pass
            self._poll_live_summary(dev_id)
            self._emit_printer_ready(dev_id)
            self._start_heartbeat(dev_id)

        threading.Timer(0.05, _notify_connected).start()
        return ORCA_SUCCESS

    def disconnect_printer(self) -> int:
        self._stop_heartbeat()
        return ORCA_SUCCESS

    def start_discovery(self, start: bool, sending: bool) -> bool:
        if start:
            self.announce_printers()
        return True

    def bind_detect(self, dev_ip: str, sec_link: str, detect: Any) -> int:
        return ORCA_ERROR_GENERIC

    def send_message(self, dev_id: str, json_str: str, qos: int, flag: int) -> int:
        self._handle_orca_command(dev_id, json_str)
        return ORCA_SUCCESS

    def send_message_to_printer(self, dev_id: str, json_str: str, qos: int, flag: int) -> int:
        self._handle_orca_command(dev_id, json_str)
        return ORCA_SUCCESS

    def _dispatch_cloud_control(self, dev_id: str, cmd: str, param: Any = None) -> None:
        cfg = load_config(path=_CONFIG_FILE)
        if not cfg.token:
            return
        pid_str = str(dev_id or self._selected_dev_id or (cfg.printer_id or ""))
        if not pid_str.isdigit():
            return
        pid = int(pid_str)
        cached = self._summaries.get(pid)
        project_id = cached.project_id if cached and cached.printer_id == pid else 0
        try:
            client = AnycubicCloudClient(token=cfg.token)
            if cmd == "pause":
                client.pause_print(printer_id=pid, project_id=project_id)
            elif cmd == "resume":
                client.resume_print(printer_id=pid, project_id=project_id)
            elif cmd == "stop":
                client.stop_print(printer_id=pid, project_id=project_id)
            elif cmd == "print_speed":
                # OrcaSlicer speed levels: 1=Silent, 2=Standard, 3=Sport
                speed_val = int(str(param)) if str(param).isdigit() else 2
                speed_mode = max(1, min(3, speed_val))
                client.update_print_settings(
                    printer_id=pid,
                    project_id=project_id,
                    settings={"print_speed_mode": speed_mode},
                )
            summary = self._poll_live_summary(pid_str)
            self._emit_push_status(pid_str, summary=summary)
            print(
                f"[OrcaCubic] Control ok: cmd={cmd} printer={pid} project_id={project_id}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as exc:
            print(
                f"[OrcaCubic] Control failed: cmd={cmd} printer={pid} project_id={project_id} err={exc}",
                file=sys.stderr,
                flush=True,
            )

    def _handle_orca_command(self, dev_id: str, json_str: str) -> None:
        try:
            msg = json.loads(json_str) if json_str else {}
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        info = msg.get("info")
        pushing = msg.get("pushing")
        print_cmd = msg.get("print")

        if (isinstance(info, dict) and info.get("command") == "get_version") or (
            isinstance(pushing, dict) and pushing.get("command") in ("pushall", "start")
        ):
            seq = (
                (info.get("sequence_id") if isinstance(info, dict) else None)
                or (pushing.get("sequence_id") if isinstance(pushing, dict) else None)
                or "1"
            )
            def _refresh_and_emit() -> None:
                self._poll_live_summary(dev_id)
                self._emit_printer_ready(dev_id, str(seq))

            threading.Thread(target=_refresh_and_emit, daemon=True).start()
            return

        if isinstance(print_cmd, dict):
            cmd = str(print_cmd.get("command") or "")
            if cmd in ("pause", "resume", "stop", "print_speed"):
                param = print_cmd.get("param")
                threading.Thread(
                    target=lambda: self._dispatch_cloud_control(dev_id, cmd, param),
                    daemon=True,
                ).start()

    def get_camera_url(self) -> str:
        return ""

    def fetch_filament_info(self, dev_id: str, sync_mode: Any) -> bool:
        return False

    def check_cert(self) -> int:
        return ORCA_SUCCESS

    def install_device_cert(self, dev_id: str, lan_only: bool) -> None:
        return None

    def ping_bind(self, ping_code: str) -> int:
        return ORCA_ERROR_GENERIC

    def bind(self, *args: Any) -> int:
        return ORCA_ERROR_GENERIC

    def unbind(self, dev_id: str) -> int:
        return ORCA_ERROR_GENERIC

    def get_hms_snapshot(self, dev_id: str, file_name: str, callback: Any) -> int:
        return ORCA_ERROR_GENERIC

    def request_bind_ticket(self) -> tuple:
        return (ORCA_ERROR_GENERIC, "")

    def set_server_callback(self, fn: Any) -> int:
        self._callbacks["server"] = fn
        return ORCA_SUCCESS

    def set_on_ssdp_msg_fn(self, fn: Any) -> int:
        self._callbacks["ssdp"] = fn
        if callable(fn):
            self.announce_printers()
        return ORCA_SUCCESS

    def set_on_printer_connected_fn(self, fn: Any) -> int:
        self._callbacks["connected"] = fn
        return ORCA_SUCCESS

    def set_on_subscribe_failure_fn(self, fn: Any) -> int:
        self._callbacks["subscribe_failure"] = fn
        return ORCA_SUCCESS

    def set_on_message_fn(self, fn: Any) -> int:
        self._callbacks["message"] = fn
        return ORCA_SUCCESS

    def set_on_user_message_fn(self, fn: Any) -> int:
        self._callbacks["user_message"] = fn
        return ORCA_SUCCESS

    def set_on_local_connect_fn(self, fn: Any) -> int:
        self._callbacks["local_connect"] = fn
        return ORCA_SUCCESS

    def set_on_local_message_fn(self, fn: Any) -> int:
        self._callbacks["local_message"] = fn
        return ORCA_SUCCESS

    def set_queue_on_main_fn(self, fn: Any) -> int:
        self._callbacks["queue_on_main"] = fn
        return ORCA_SUCCESS

    def command_ams_refresh_rfid(self, *args: Any) -> int:
        return -7010

    def command_ams_calibrate(self, *args: Any) -> int:
        return -7010

    def command_ams_select_tray(self, *args: Any) -> int:
        return -7010

    def command_start_camera(self, *args: Any) -> int:
        return -7010

    def command_xyz_abs(self, *args: Any) -> int:
        return -7010

    def command_auto_leveling(self, *args: Any) -> int:
        return -7010

    def command_go_home(self, *args: Any) -> int:
        return -7010

    def command_set_bed(self, *args: Any) -> int:
        return -7010

    def command_set_nozzle(self, *args: Any) -> int:
        return -7010

    def command_axis_control(self, *args: Any) -> int:
        return -7010

    def start_local_print_with_record(
        self, params: Any, update_fn: Any, cancel_fn: Any, wait_fn: Any = None
    ) -> int:
        return self.start_print(params, update_fn, cancel_fn, wait_fn)

    def start_sdcard_print(
        self, params: Any, update_fn: Any, cancel_fn: Any
    ) -> int:
        return self.start_print(params, update_fn, cancel_fn)

    def get_user_selected_machine(self) -> str:
        if self._selected_dev_id:
            return self._selected_dev_id
        cfg = load_config(path=_CONFIG_FILE)
        return str(cfg.printer_id) if cfg.printer_id is not None else ""

    def set_user_selected_machine(self, dev_id: str) -> int:
        self._selected_dev_id = str(dev_id or "")
        return ORCA_SUCCESS

    def _run_job(
        self,
        params: Any,
        force_upload_only: bool,
        update_fn: Any = None,
        cancel_fn: Any = None,
    ) -> int:
        def report(pct: int, msg: str) -> None:
            pct_i = max(0, min(100, int(pct)))
            if callable(update_fn):
                # OrcaSlicer PrintJob::update_fn(stage, code, info):
                # stage 0=Create(20%), 1=Upload(30..70% via code 0..100 + info),
                # 4=Sending(97%), 5=Finished(100%).
                if pct_i < 10:
                    stage, code, info = 0, 0, str(msg)
                elif pct_i < 88:
                    upload_pct = max(0, min(100, round((pct_i - 10) * 100 / 78)))
                    stage, code, info = 1, upload_pct, str(msg)
                elif pct_i < 100:
                    stage, code, info = 4, 0, str(msg)
                else:
                    stage, code, info = 5, 0, "1"
                try:
                    update_fn(stage, code, info)
                except TypeError:
                    try:
                        update_fn(pct_i, str(msg))
                    except Exception:
                        pass
            if _ACTIVE_PAGE is not None:
                try:
                    _ACTIVE_PAGE.post_message(
                        {
                            "type": "upload_progress",
                            "active": pct_i < 100,
                            "pct": pct_i,
                            "text": str(msg),
                        }
                    )
                except Exception:
                    pass

        def is_cancelled() -> bool:
            if callable(cancel_fn):
                try:
                    return bool(cancel_fn())
                except Exception:
                    return False
            return False

        cfg = load_config(path=_CONFIG_FILE)
        if not cfg.token:
            report(
                0,
                "Требуется вход в Anycubic Cloud (откройте вкладку OrcaCubic).",
            )
            return ORCA_ERROR_AUTH

        tmp_to_cleanup: Optional[Path] = None
        try:
            gcode_path, tmp_to_cleanup = _extract_gcode_path(params)
            remote_name = _extract_remote_name(params, gcode_path)
            if remote_name.lower().startswith("verify_job"):
                print(
                    f"[OrcaCubic] Skipping OrcaSlicer probe file: {remote_name}",
                    file=sys.stderr,
                    flush=True,
                )
                return ORCA_SUCCESS
            print(
                f"[OrcaCubic] Starting job: file={remote_name} size={gcode_path.stat().st_size} "
                f"upload_only={force_upload_only} mode={cfg.default_mode}",
                file=sys.stderr,
                flush=True,
            )
            client = AnycubicCloudClient(token=cfg.token)

            report(2, "Поиск принтера в Anycubic Cloud...")
            param_dev_id = getattr(params, "dev_id", "")
            if isinstance(param_dev_id, str) and param_dev_id.strip().isdigit():
                cfg.printer_id = int(param_dev_id.strip())
            printer = _resolve_target_printer(client, cfg)

            should_start_print = not force_upload_only
            record_timelapse = bool(getattr(params, "task_record_timelapse", False))
            res = client.upload_and_print(
                printer_id=printer.printer_id,
                file_path=gcode_path,
                remote_name=remote_name,
                start_print=should_start_print,
                camera_timelapse=record_timelapse,
                progress_cb=report,
                cancel_cb=is_cancelled,
            )
            print(
                f"[OrcaCubic] Job finished: printer={printer.printer_id} file_id={res.get('file_id')} "
                f"started={res.get('started')}",
                file=sys.stderr,
                flush=True,
            )
            action_text = (
                f"Печать запущена на {printer.name}"
                if should_start_print
                else f"Файл загружен в Anycubic Cloud ({printer.name})"
            )
            report(100, action_text)
            if _ACTIVE_PAGE is not None and not should_start_print:
                _ACTIVE_PAGE.trigger_files_refresh(notice=action_text)
            return ORCA_SUCCESS
        except AnycubicTokenExpiredError as exc:
            print(f"[OrcaCubic] Token expired: {exc}", file=sys.stderr, flush=True)
            report(0, str(exc))
            return ORCA_ERROR_AUTH
        except AnycubicAuthError as exc:
            print(f"[OrcaCubic] Auth error: {exc}", file=sys.stderr, flush=True)
            report(0, str(exc))
            return ORCA_ERROR_AUTH
        except AnycubicError as exc:
            msg = str(exc)
            print(f"[OrcaCubic] Cloud error: {msg}", file=sys.stderr, flush=True)
            report(0, msg)
            if "отменена" in msg.lower():
                return ORCA_ERROR_CANCELLED
            return ORCA_ERROR_GENERIC
        except Exception as exc:
            print(f"[OrcaCubic] Unexpected error: {exc!r}", file=sys.stderr, flush=True)
            report(0, f"Неожиданная ошибка OrcaCubic: {exc}")
            return ORCA_ERROR_GENERIC
        finally:
            if _ACTIVE_PAGE is not None:
                try:
                    _ACTIVE_PAGE.post_message(
                        {"type": "upload_progress", "active": False, "pct": 100, "text": ""}
                    )
                except Exception:
                    pass
            if tmp_to_cleanup is not None:
                try:
                    tmp_to_cleanup.write_bytes(b"")
                except OSError:
                    pass

    def start_print(
        self, params: Any, update_fn: Any, cancel_fn: Any, wait_fn: Any = None
    ) -> int:
        return self._run_job(
            params=params,
            force_upload_only=False,
            update_fn=update_fn,
            cancel_fn=cancel_fn,
        )

    def start_local_print(
        self, params: Any, update_fn: Any, cancel_fn: Any
    ) -> int:
        return self._run_job(
            params=params,
            force_upload_only=False,
            update_fn=update_fn,
            cancel_fn=cancel_fn,
        )

    def start_send_gcode_to_sdcard(
        self, params: Any, update_fn: Any, cancel_fn: Any, wait_fn: Any = None
    ) -> int:
        return self._run_job(
            params=params,
            force_upload_only=True,
            update_fn=update_fn,
            cancel_fn=cancel_fn,
        )


_PAGE_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>OrcaCubic — Anycubic Cloud</title>
<style>
  :root {
    --bg: #1e1f22;
    --card: #2b2d31;
    --border: #3c3f45;
    --text: #eef0f4;
    --muted: #9aa0aa;
    --accent: #00b894;
    --accent-hover: #00a383;
    --danger: #e17055;
    --warn: #fdcb6e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 20px;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.45;
  }
  .wrap { max-width: 720px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  h1 { margin: 0 0 4px 0; font-size: 20px; }
  h2 { margin: 0 0 12px 0; font-size: 15px; color: var(--text); }
  .sub { color: var(--muted); font-size: 13px; margin: 0; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  button {
    background: var(--accent);
    color: #0b1412;
    border: none;
    border-radius: 7px;
    padding: 8px 14px;
    font-weight: 600;
    cursor: pointer;
    font-size: 13px;
  }
  button:hover { background: var(--accent-hover); }
  button.secondary {
    background: #3a3d44;
    color: var(--text);
  }
  button.secondary:hover { background: #484c55; }
  button.danger { background: var(--danger); color: #fff; }
  button:disabled { opacity: 0.55; cursor: not-allowed; }
  input[type="password"], input[type="text"], select {
    background: #1a1b1e;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 7px;
    padding: 8px 10px;
    font-size: 13px;
    flex: 1;
    min-width: 200px;
  }
  .banner {
    padding: 10px 12px;
    border-radius: 7px;
    font-size: 13px;
    display: none;
  }
  .banner.show { display: block; }
  .banner.err { background: rgba(225,112,85,0.16); border: 1px solid var(--danger); color: #ffb3a1; }
  .banner.ok { background: rgba(0,184,148,0.15); border: 1px solid var(--accent); color: #9df3df; }
  .banner.info { background: rgba(253,203,110,0.14); border: 1px solid var(--warn); color: #ffe6a8; }
  .printer-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    margin-bottom: 8px;
    cursor: pointer;
  }
  .printer-item.selected { border-color: var(--accent); background: rgba(0,184,148,0.08); }
  .badge {
    font-size: 12px;
    padding: 3px 8px;
    border-radius: 999px;
    font-weight: 600;
  }
  .badge.idle { background: rgba(0,184,148,0.2); color: #55efc4; }
  .badge.downloading { background: rgba(116,185,255,0.2); color: #74b9ff; }
  .badge.printing { background: rgba(253,203,110,0.2); color: #ffeaa7; }
  .badge.paused { background: rgba(253,203,110,0.25); color: #ffeaa7; }
  .badge.offline { background: rgba(225,112,85,0.2); color: #fab1a0; }
  .progress-track {
    width: 100%;
    height: 10px;
    background: #1a1b1e;
    border-radius: 999px;
    overflow: hidden;
    margin: 10px 0;
  }
  .progress-fill {
    height: 100%;
    background: var(--accent);
    width: 0%;
    transition: width 0.3s ease;
  }
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 8px;
    margin: 12px 0;
  }
  .stat-box {
    background: #1a1b1e;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 9px 10px;
  }
  .stat-label { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .stat-val { font-size: 15px; font-weight: 700; margin-top: 2px; white-space: nowrap; }
  details { margin-top: 10px; }
  summary { cursor: pointer; color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="row" style="justify-content: space-between;">
      <div>
        <h1>OrcaCubic — Anycubic Cloud</h1>
        <p class="sub" id="authStatus" data-i18n="checking_status">Проверка состояния...</p>
      </div>
      <div class="row">
        <button id="btnLang" class="secondary" style="padding:7px 10px; font-size:12px;" onclick="toggleLanguage()" title="Switch language / Сменить язык">EN / RU</button>
        <button id="btnLogin" data-i18n="btn_login" onclick="send({action:'login_window'})">Войти в Anycubic Cloud</button>
        <button id="btnRelogin" class="secondary" data-i18n="btn_relogin" onclick="send({action:'login_window', reset_session:true})">Сменить аккаунт</button>
        <button id="btnLogout" class="secondary" data-i18n="btn_logout" onclick="send({action:'logout'})">Выйти</button>
      </div>
    </div>
    <details id="manualTokenBox">
      <summary data-i18n="manual_token_summary">Войти через браузер / вставить XX-Token вручную (Windows / Linux / macOS)</summary>
      <p class="sub" id="manualTokenSteps" style="margin: 10px 0 8px 0;">
        1. Откройте <a href="https://cloud-universe.anycubic.com/" target="_blank" style="color:var(--accent)">cloud-universe.anycubic.com</a> и войдите в аккаунт.<br>
        2. В консоли браузера (F12 → Console) выполните: <code>copy(localStorage.getItem('XX-Token'))</code> и вставьте ниже:
      </p>
      <div class="row">
        <input id="manualToken" type="password" data-i18n-placeholder="manual_token_ph" placeholder="Вставьте XX-Token" />
        <button class="secondary" data-i18n="btn_save_token" onclick="saveManualToken()">Сохранить токен</button>
      </div>
    </details>
  </div>

  <div id="banner" class="banner"></div>

  <div class="card" id="monitorCard" style="display:none;">
    <div class="row" style="justify-content: space-between; margin-bottom: 6px;">
      <h2 style="margin:0;" id="monitorTitle" data-i18n="monitor_title">Мониторинг печати</h2>
      <span id="monitorBadge" class="badge idle">Idle</span>
    </div>
    <div id="monitorFile" class="sub" data-i18n="no_active_job">Нет активного задания</div>
    <div class="progress-track"><div id="monitorProgressBar" class="progress-fill"></div></div>
    <div class="stats-grid">
      <div class="stat-box">
        <div class="stat-label" data-i18n="stat_progress">Прогресс</div>
        <div class="stat-val" id="statProgress">0%</div>
      </div>
      <div class="stat-box">
        <div class="stat-label" data-i18n="stat_layers">Слои</div>
        <div class="stat-val" id="statLayers">—</div>
      </div>
      <div class="stat-box">
        <div class="stat-label" data-i18n="stat_remain">Осталось / ETA</div>
        <div class="stat-val" id="statRemain">—</div>
      </div>
      <div class="stat-box">
        <div class="stat-label" data-i18n="stat_nozzle">Сопло</div>
        <div class="stat-val" id="statNozzle">—</div>
      </div>
      <div class="stat-box">
        <div class="stat-label" data-i18n="stat_bed">Стол</div>
        <div class="stat-val" id="statBed">—</div>
      </div>
    </div>
    <div class="row" style="margin-top: 10px;">
      <button id="btnPause" class="secondary" data-i18n="btn_pause" onclick="controlPrinter('pause')">Пауза</button>
      <button id="btnResume" data-i18n="btn_resume" onclick="controlPrinter('resume')" style="display:none;">Продолжить</button>
      <button id="btnStop" class="danger" data-i18n="btn_stop" onclick="controlPrinter('stop')">Стоп</button>
      <span style="flex:1;"></span>
      <select id="speedSelect" style="flex:0 0 auto; min-width:150px;" onchange="changeSpeed(this.value)">
        <option value="" data-i18n="speed_placeholder">Скорость печати...</option>
        <option value="1" data-i18n="speed_quiet">Тихий режим</option>
        <option value="2" data-i18n="speed_std">Стандартный</option>
        <option value="3" data-i18n="speed_sport">Спорт</option>
      </select>
      <button id="btnCamStart" class="secondary" data-i18n="btn_cam_start" onclick="startCamera()">Включить камеру</button>
      <button id="btnCamStop" class="secondary" data-i18n="btn_cam_stop" onclick="stopCamera()" style="display:none;">Выключить камеру</button>
    </div>
    <div id="cameraBox" style="display:none; margin-top: 12px;">
      <div id="camViewport" style="width:100%; aspect-ratio:4/3; max-height:420px; margin:0 auto; background:#000; border-radius:8px; overflow:hidden; position:relative;">
        <video id="camVideo" autoplay playsinline muted style="width:100%; height:100%; object-fit:cover; display:block; background:#000; transform-origin:center center; transition:transform 0.08s linear;"></video>
      </div>
      <div class="row" style="margin-top:8px; gap:8px;">
        <span class="sub" data-i18n="cam_horizon">Горизонт:</span>
        <input id="camAngle" type="range" min="-45" max="45" step="0.5" value="0" style="flex:1; min-width:120px;" oninput="updateCamTransform(false)" onchange="updateCamTransform(true)" />
        <span id="camAngleVal" class="sub" style="min-width:44px; text-align:right;">0°</span>
        <button class="secondary" style="padding:4px 8px;" onclick="rotateCam90()" title="Rotate 90°">+90°</button>
        <button class="secondary" style="padding:4px 8px;" onclick="toggleCamFlip('h')" title="Flip horizontal">⇄</button>
        <button class="secondary" style="padding:4px 8px;" onclick="toggleCamFlip('v')" title="Flip vertical">⇅</button>
        <button class="secondary" style="padding:4px 8px;" onclick="toggleCamFullscreen()" title="Fullscreen">⛶</button>
        <button class="secondary" style="padding:4px 8px;" data-i18n="cam_reset" onclick="resetCamTransform()">Сброс</button>
      </div>
      <details style="margin-top: 8px;">
        <summary data-i18n="tl_summary">Таймлапс (опционально)</summary>
        <div class="row" style="margin-top:8px; gap:8px;">
          <button id="btnTlStart" class="secondary" style="padding:5px 10px;" data-i18n="btn_tl_start" onclick="startTimelapse()">● Записать таймлапс</button>
          <button id="btnTlStop" class="danger" style="padding:5px 10px; display:none;" data-i18n="btn_tl_stop" onclick="stopTimelapse(true)">■ Сохранить таймлапс</button>
          <select id="tlInterval" style="flex:0 0 auto; min-width:170px; padding:5px 8px;">
            <option value="2000" data-i18n="tl_2s">Кадр раз в 2с</option>
            <option value="5000" selected data-i18n="tl_5s">Кадр раз в 5с</option>
            <option value="10000" data-i18n="tl_10s">Кадр раз в 10с</option>
            <option value="30000" data-i18n="tl_30s">Кадр раз в 30с</option>
            <option value="layer" data-i18n="tl_layer">По смене слоя</option>
            <option value="park" data-i18n="tl_park">При парковке головы</option>
          </select>
          <span id="tlStatus" class="sub"></span>
        </div>
        <div class="row" style="margin-top:8px; gap:8px;">
          <label style="display:flex; align-items:center; gap:6px; font-size:13px; color:var(--muted); cursor:pointer;">
            <input id="tlAutoCheckbox" type="checkbox" onchange="toggleTimelapseAuto(this.checked)" />
            <span data-i18n="tl_auto_label">Автозапуск и автосохранение таймлапса при печати (при включённой камере)</span>
          </label>
        </div>
        <div class="row" style="margin-top:8px; gap:8px;">
          <input id="tlDirInput" type="text" data-i18n-placeholder="tl_dir_ph" placeholder="Папка сохранения (по умолчанию data/timelapses)" />
          <button class="secondary" style="padding:5px 10px;" data-i18n="btn_browse" onclick="send({action:'browse_timelapse_dir'})">Выбрать папку...</button>
          <button class="secondary" style="padding:5px 10px;" data-i18n="btn_save" onclick="saveTimelapseDir()">Сохранить</button>
        </div>
      </details>
      <div id="camStatus" class="sub" style="margin-top:6px;"></div>
    </div>
  </div>

  <div class="card">
    <h2 data-i18n="printers_title">Принтеры аккаунта</h2>
    <div class="row" style="margin-bottom: 10px;">
      <input id="printerSearch" type="text" data-i18n-placeholder="printer_search_ph" placeholder="Поиск по имени, модели или ID..." oninput="applyPrinterFilter()" />
      <select id="printerStatusFilter" style="flex:0 0 auto; min-width:140px;" onchange="applyPrinterFilter()">
        <option value="all" data-i18n="flt_all">Все статусы</option>
        <option value="idle" data-i18n="flt_idle">Свободен</option>
        <option value="printing" data-i18n="flt_printing">Печатает</option>
        <option value="online" data-i18n="flt_online">Онлайн</option>
      </select>
      <button id="btnRefresh" class="secondary" data-i18n="btn_refresh" onclick="refreshPrinters()">Обновить</button>
    </div>
    <div id="lastUpdated" class="sub" style="margin-bottom: 10px;"></div>
    <div id="printersList" class="sub" data-i18n="login_to_see_printers">Войдите в аккаунт, чтобы увидеть принтеры.</div>
    <p class="sub" id="modeHint" style="margin-top: 10px;">
      • <strong>Распечатать стол</strong> — отправить на печать через Anycubic Cloud.<br>
      • <strong>Отправить</strong> — только загрузить .gcode в облачную библиотеку Anycubic.
    </p>
  </div>

  <div class="card">
    <h2 data-i18n="cloud_files_title">Облачные файлы Anycubic Cloud</h2>
    <div class="row" style="margin-bottom: 12px;">
      <button id="btnRefreshFiles" class="secondary" data-i18n="btn_refresh_files" onclick="refreshFiles()">Обновить файлы</button>
      <span id="cloudSpace" class="sub"></span>
    </div>
    <div id="cloudFilesList" class="sub" data-i18n="login_to_see_files">Войдите в аккаунт, чтобы увидеть файлы в облаке.</div>
  </div>
</div>

<script>
  const I18N = {
    en: {
      checking_status: 'Checking status...',
      btn_login: 'Sign in to Anycubic Cloud',
      btn_relogin: 'Switch account',
      btn_logout: 'Sign out',
      manual_token_summary: 'Sign in via browser / paste XX-Token manually (Windows / Linux / macOS)',
      manual_token_steps: '1. Open <a href="https://cloud-universe.anycubic.com/" target="_blank" style="color:var(--accent)">cloud-universe.anycubic.com</a> and sign in.<br>2. In DevTools Console (F12 → Console) run: <code>copy(localStorage.getItem("XX-Token"))</code> and paste below:',
      manual_token_ph: 'Paste XX-Token',
      btn_save_token: 'Save token',
      monitor_title: 'Print Monitor',
      no_active_job: 'No active print job',
      stat_progress: 'Progress',
      stat_layers: 'Layers',
      stat_remain: 'Remaining / ETA',
      stat_nozzle: 'Nozzle',
      stat_bed: 'Bed',
      btn_pause: 'Pause',
      btn_resume: 'Resume',
      btn_stop: 'Stop',
      speed_placeholder: 'Print speed...',
      speed_quiet: 'Quiet',
      speed_std: 'Standard',
      speed_sport: 'Sport',
      btn_cam_start: 'Start camera',
      btn_cam_stop: 'Stop camera',
      cam_horizon: 'Horizon:',
      cam_reset: 'Reset',
      tl_summary: 'Timelapse (optional)',
      btn_tl_start: '● Record timelapse',
      btn_tl_stop: '■ Save timelapse',
      tl_2s: 'Frame every 2s',
      tl_5s: 'Frame every 5s',
      tl_10s: 'Frame every 10s',
      tl_30s: 'Frame every 30s',
      tl_layer: 'On layer change',
      tl_park: 'On head park',
      tl_auto_label: 'Auto-start & auto-save timelapse on print (when camera is on)',
      tl_dir_ph: 'Output folder (default: data/timelapses)',
      btn_browse: 'Browse...',
      btn_save: 'Save',
      printers_title: 'Account Printers',
      printer_search_ph: 'Search by name, model, or ID...',
      flt_all: 'All statuses',
      flt_idle: 'Idle',
      flt_printing: 'Printing',
      flt_online: 'Online',
      btn_refresh: 'Refresh',
      btn_refreshing: 'Refreshing...',
      login_to_see_printers: 'Sign in to view your printers.',
      mode_hint: '• <strong>Print plate</strong> — upload and start printing via Anycubic Cloud.<br>• <strong>Send</strong> — upload .gcode to Anycubic Cloud library only.',
      cloud_files_title: 'Anycubic Cloud Files',
      btn_refresh_files: 'Refresh files',
      btn_loading: 'Loading...',
      login_to_see_files: 'Sign in to view cloud files.',
      status_idle: 'Idle',
      status_downloading: 'Downloading file',
      status_printing: 'Printing',
      status_paused: 'Paused',
      status_offline: 'Offline',
      file_prefix: 'File: ',
      fw_prefix: 'Firmware: ',
      updated_prefix: 'Updated: ',
      used_prefix: 'Storage: ',
      signed_in: 'Signed in',
      not_signed_in_helper: 'Not signed in. Click "Sign in to Anycubic Cloud".',
      not_signed_in_manual: 'Not signed in. Paste XX-Token from cloud-universe.anycubic.com below.',
      no_printers: 'No printers found. Click "Refresh".',
      no_printers_filter: 'No printers match the current filter.',
      no_files: 'No cloud files yet. Click "Refresh files".',
      btn_print: 'Print',
      btn_delete: 'Delete',
      nozzle_short: 'Nozzle',
      bed_short: 'Bed',
      min_short: 'min',
      confirm_print: 'Start printing "{name}" on the selected printer?',
      confirm_delete: 'Delete "{name}" from Anycubic Cloud?',
      confirm_stop: 'Stop the current print on the selected printer?',
      cam_reconnecting: 'Reconnecting to Anycubic Cloud camera...',
      cam_connecting: 'Connecting to Anycubic Cloud camera...',
      cam_ws_connecting: 'Connecting WebSocket to AWS KVS ({region})...',
      cam_active: 'Camera active ({region})',
      cam_ice_checking: 'Establishing video stream (ICE checking)...',
      cam_dropped_retry: 'Camera connection lost ({st}), reconnecting ({n}/2)...',
      cam_dropped_final: 'Camera connection lost ({st}). Click "Stop camera" and "Start camera".',
      cam_waiting_printer: 'Waiting for printer response (attempt {n}/3)...',
      cam_ws_open: 'WebSocket open, sending SDP_OFFER...',
      cam_sdp_answer: 'Printer answered (SDP_ANSWER), starting video...',
      cam_ws_closed: 'WebSocket closed (code {code}{reason}).',
      cam_ws_error: 'WebSocket error connecting to AWS KVS.',
      cam_webrtc_error: 'WebRTC error: ',
      cam_failed: 'Failed to start camera.',
      tl_recording: 'Recording: {n} frames',
      tl_wait_video: 'Wait for camera video to appear first.',
      tl_no_recorder: 'MediaRecorder is not supported in this WebView.',
      tl_no_frames: 'Failed to capture frames.',
      tl_saving: 'Saving timelapse ({n} frames)...',
      tl_encode_err: 'Failed to encode timelapse.',
      tl_layer_mode: 'Layer-change mode: {n} frames',
      tl_park_wait: 'Waiting for head park...',
      tl_saved_default: 'Timelapse saved.',
      enter_token: 'Enter XX-Token.'
    },
    ru: {
      checking_status: 'Проверка состояния...',
      btn_login: 'Войти в Anycubic Cloud',
      btn_relogin: 'Сменить аккаунт',
      btn_logout: 'Выйти',
      manual_token_summary: 'Войти через браузер / вставить XX-Token вручную (Windows / Linux / macOS)',
      manual_token_steps: '1. Откройте <a href="https://cloud-universe.anycubic.com/" target="_blank" style="color:var(--accent)">cloud-universe.anycubic.com</a> и войдите в аккаунт.<br>2. В консоли браузера (F12 → Console) выполните: <code>copy(localStorage.getItem("XX-Token"))</code> и вставьте ниже:',
      manual_token_ph: 'Вставьте XX-Token',
      btn_save_token: 'Сохранить токен',
      monitor_title: 'Мониторинг печати',
      no_active_job: 'Нет активного задания',
      stat_progress: 'Прогресс',
      stat_layers: 'Слои',
      stat_remain: 'Осталось / ETA',
      stat_nozzle: 'Сопло',
      stat_bed: 'Стол',
      btn_pause: 'Пауза',
      btn_resume: 'Продолжить',
      btn_stop: 'Стоп',
      speed_placeholder: 'Скорость печати...',
      speed_quiet: 'Тихий режим',
      speed_std: 'Стандартный',
      speed_sport: 'Спорт',
      btn_cam_start: 'Включить камеру',
      btn_cam_stop: 'Выключить камеру',
      cam_horizon: 'Горизонт:',
      cam_reset: 'Сброс',
      tl_summary: 'Таймлапс (опционально)',
      btn_tl_start: '● Записать таймлапс',
      btn_tl_stop: '■ Сохранить таймлапс',
      tl_2s: 'Кадр раз в 2с',
      tl_5s: 'Кадр раз в 5с',
      tl_10s: 'Кадр раз в 10с',
      tl_30s: 'Кадр раз в 30с',
      tl_layer: 'По смене слоя',
      tl_park: 'При парковке головы',
      tl_auto_label: 'Автозапуск и автосохранение таймлапса при печати (при включённой камере)',
      tl_dir_ph: 'Папка сохранения (по умолчанию data/timelapses)',
      btn_browse: 'Выбрать папку...',
      btn_save: 'Сохранить',
      printers_title: 'Принтеры аккаунта',
      printer_search_ph: 'Поиск по имени, модели или ID...',
      flt_all: 'Все статусы',
      flt_idle: 'Свободен',
      flt_printing: 'Печатает',
      flt_online: 'Онлайн',
      btn_refresh: 'Обновить',
      btn_refreshing: 'Обновление...',
      login_to_see_printers: 'Войдите в аккаунт, чтобы увидеть принтеры.',
      mode_hint: '• <strong>Распечатать стол</strong> — отправить на печать через Anycubic Cloud.<br>• <strong>Отправить</strong> — только загрузить .gcode в облачную библиотеку Anycubic.',
      cloud_files_title: 'Облачные файлы Anycubic Cloud',
      btn_refresh_files: 'Обновить файлы',
      btn_loading: 'Загрузка...',
      login_to_see_files: 'Войдите в аккаунт, чтобы увидеть файлы в облаке.',
      status_idle: 'Свободен',
      status_downloading: 'Загрузка файла',
      status_printing: 'Печатает',
      status_paused: 'Пауза',
      status_offline: 'Офлайн',
      file_prefix: 'Файл: ',
      fw_prefix: 'Прошивка: ',
      updated_prefix: 'Обновлено: ',
      used_prefix: 'Занято: ',
      signed_in: 'Авторизован',
      not_signed_in_helper: 'Не авторизован. Нажмите «Войти в Anycubic Cloud».',
      not_signed_in_manual: 'Не авторизован. Вставьте XX-Token из cloud-universe.anycubic.com ниже.',
      no_printers: 'Принтеры не найдены. Нажмите «Обновить».',
      no_printers_filter: 'По фильтру принтеры не найдены.',
      no_files: 'В облаке пока нет файлов. Нажмите «Обновить файлы».',
      btn_print: 'Печатать',
      btn_delete: 'Удалить',
      nozzle_short: 'Сопло',
      bed_short: 'Стол',
      min_short: 'мин',
      confirm_print: 'Запустить печать «{name}» на выбранном принтере?',
      confirm_delete: 'Удалить «{name}» из Anycubic Cloud?',
      confirm_stop: 'Остановить текущую печать на выбранном принтере?',
      cam_reconnecting: 'Переподключение к камере Anycubic Cloud...',
      cam_connecting: 'Подключение к камере Anycubic Cloud...',
      cam_ws_connecting: 'Подключение WebSocket к AWS KVS ({region})...',
      cam_active: 'Камера активна ({region})',
      cam_ice_checking: 'Установка видеопотока (ICE checking)...',
      cam_dropped_retry: 'Связь с камерой прервалась ({st}), переподключение ({n}/2)...',
      cam_dropped_final: 'Соединение с камерой прервано ({st}). Нажмите «Выключить» и «Включить камеру».',
      cam_waiting_printer: 'Ожидание ответа от принтера (попытка {n}/3)...',
      cam_ws_open: 'WebSocket открыт, отправка SDP_OFFER...',
      cam_sdp_answer: 'Принтер ответил (SDP_ANSWER), запуск видео...',
      cam_ws_closed: 'WebSocket закрыт (код {code}{reason}).',
      cam_ws_error: 'Ошибка WebSocket подключения к AWS KVS.',
      cam_webrtc_error: 'Ошибка WebRTC: ',
      cam_failed: 'Не удалось включить камеру.',
      tl_recording: 'Идёт запись: {n} кадр.',
      tl_wait_video: 'Сначала дождитесь появления видео с камеры.',
      tl_no_recorder: 'MediaRecorder не поддерживается в этом WebView.',
      tl_no_frames: 'Не удалось записать кадры.',
      tl_saving: 'Сохранение таймлапса ({n} кадр.)...',
      tl_encode_err: 'Ошибка кодирования таймлапса.',
      tl_layer_mode: 'Режим «По смене слоя»: {n} кадр.',
      tl_park_wait: 'Ожидание парковки головы...',
      tl_saved_default: 'Таймлапс сохранён.',
      enter_token: 'Введите XX-Token.'
    }
  };
  let _lang = (navigator.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en';
  function t(key) {
    return (I18N[_lang] && I18N[_lang][key]) || I18N.en[key] || key;
  }
  function applyLanguage(lang) {
    if (lang === 'en' || lang === 'ru') _lang = lang;
    else _lang = (navigator.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en';
    document.querySelectorAll('[data-i18n]').forEach(function(el) {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function(el) {
      el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
    });
    const stepsEl = document.getElementById('manualTokenSteps');
    if (stepsEl) stepsEl.innerHTML = t('manual_token_steps');
    const hintEl = document.getElementById('modeHint');
    if (hintEl) hintEl.innerHTML = t('mode_hint');
    const btnLang = document.getElementById('btnLang');
    if (btnLang) btnLang.textContent = _lang === 'ru' ? 'EN' : 'RU';
  }
  function toggleLanguage() {
    const next = _lang === 'ru' ? 'en' : 'ru';
    applyLanguage(next);
    send({action: 'set_language', language: next});
    if (_lastState) renderState(_lastState);
  }
  function send(payload) {
    if (window.orca && typeof window.orca.postMessage === 'function') {
      window.orca.postMessage(payload);
    }
  }
  function showBanner(kind, text) {
    const el = document.getElementById('banner');
    if (!text) { el.className = 'banner'; el.textContent = ''; return; }
    el.className = 'banner show ' + kind;
    el.textContent = text;
  }
  function refreshPrinters() {
    const btn = document.getElementById('btnRefresh');
    if (btn) { btn.disabled = true; btn.textContent = t('btn_refreshing'); }
    send({action: 'refresh_printers'});
  }
  function refreshFiles() {
    const btn = document.getElementById('btnRefreshFiles');
    if (btn) { btn.disabled = true; btn.textContent = t('btn_loading'); }
    send({action: 'refresh_files'});
  }
  function printCloudFile(fileId, gcodeId, name, url) {
    if (!confirm(t('confirm_print').replace('{name}', name))) return;
    send({action: 'print_cloud_file', file_id: fileId, gcode_id: gcodeId, name: name, url: url || ''});
  }
  function deleteCloudFile(fileId, name) {
    if (!confirm(t('confirm_delete').replace('{name}', name))) return;
    send({action: 'delete_cloud_file', file_id: fileId, name: name});
  }
  function controlPrinter(cmd) {
    if (cmd === 'stop' && !confirm(t('confirm_stop'))) return;
    send({action: 'printer_control', cmd: cmd});
  }
  function changeSpeed(val) {
    if (!val) return;
    send({action: 'printer_control', cmd: 'print_speed', param: val});
    document.getElementById('speedSelect').value = '';
  }

  let _camWs = null;
  let _camPc = null;
  let _camRetryTimers = [];
  let _camTransform = { angle: 0, base_rot: 0, flip_h: false, flip_v: false };
  function applyCamTransformCss() {
    const vid = document.getElementById('camVideo');
    const slider = document.getElementById('camAngle');
    const lbl = document.getElementById('camAngleVal');
    if (!vid) return;
    const fine = Number(_camTransform.angle || 0);
    const base = Number(_camTransform.base_rot || 0);
    const totalDeg = base + fine;
    if (slider) slider.value = String(fine);
    if (lbl) lbl.textContent = (fine > 0 ? '+' : '') + fine.toFixed(1).replace(/\\.0$/, '') + '°' + (base ? (' (' + base + '°)') : '');
    // Exact inscribed-rectangle scale so rotated video fills viewport with zero black corners
    const vp = document.getElementById('camViewport');
    const ratio = (vp && vp.clientHeight > 0) ? Math.max(vp.clientWidth / vp.clientHeight, vp.clientHeight / vp.clientWidth) : 1.333;
    const rad = Math.abs(fine) * Math.PI / 180;
    const autoScale = Math.cos(rad) + Math.sin(rad) * ratio + (fine !== 0 ? 0.02 : 0);
    const sx = (_camTransform.flip_h ? -1 : 1) * autoScale;
    const sy = (_camTransform.flip_v ? -1 : 1) * autoScale;
    vid.style.transform = 'rotate(' + totalDeg + 'deg) scale(' + sx.toFixed(3) + ', ' + sy.toFixed(3) + ')';
  }
  function toggleCamFullscreen() {
    const vp = document.getElementById('camViewport');
    if (!vp) return;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(function(){});
    } else if (vp.requestFullscreen) {
      vp.requestFullscreen().catch(function(){});
    } else if (vp.webkitRequestFullscreen) {
      vp.webkitRequestFullscreen();
    }
  }
  function updateCamTransform(persist) {
    const slider = document.getElementById('camAngle');
    _camTransform.angle = slider ? parseFloat(slider.value) || 0 : 0;
    applyCamTransformCss();
    if (persist) send({action: 'save_camera_transform', transform: _camTransform});
  }
  function rotateCam90() {
    _camTransform.base_rot = ((Number(_camTransform.base_rot || 0) + 90) % 360);
    applyCamTransformCss();
    send({action: 'save_camera_transform', transform: _camTransform});
  }
  function toggleCamFlip(axis) {
    if (axis === 'h') _camTransform.flip_h = !_camTransform.flip_h;
    else _camTransform.flip_v = !_camTransform.flip_v;
    applyCamTransformCss();
    send({action: 'save_camera_transform', transform: _camTransform});
  }
  function resetCamTransform() {
    _camTransform = { angle: 0, base_rot: 0, flip_h: false, flip_v: false };
    applyCamTransformCss();
    send({action: 'save_camera_transform', transform: _camTransform});
  }
  function setCamUi(active, statusText) {
    document.getElementById('btnCamStart').style.display = active ? 'none' : '';
    document.getElementById('btnCamStop').style.display = active ? '' : 'none';
    document.getElementById('cameraBox').style.display = active ? 'block' : 'none';
    document.getElementById('camStatus').textContent = statusText || '';
  }
  let _camReconnectCount = 0;
  function startCamera(isRetry) {
    if (!isRetry) _camReconnectCount = 0;
    setCamUi(true, isRetry ? t('cam_reconnecting') : t('cam_connecting'));
    send({action: 'camera_open'});
  }
  let _tlRecorder = null;
  let _tlChunks = [];
  let _tlTimer = null;
  let _tlFrames = 0;
  let _tlCanvas = null;
  let _tlCtx = null;
  let _tlMime = 'video/mp4';
  let _tlMode = '5000';
  let _tlLastLayer = -1;
  let _tlProbeCanvas = null;
  let _tlProbeCtx = null;
  let _tlPrevFrame = null;
  let _tlSawMotion = true;
  let _tlStillTicks = 0;
  let _tlLastCaptureTs = 0;

  function _drawLeveledFrame(ctx, vid, w, h) {
    ctx.save();
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, w, h);
    ctx.translate(w / 2, h / 2);
    const fine = Number(_camTransform.angle || 0);
    const base = Number(_camTransform.base_rot || 0);
    const totalRad = (base + fine) * Math.PI / 180;
    const ratio = Math.max(w / h, h / w);
    const fineRad = Math.abs(fine) * Math.PI / 180;
    const autoScale = Math.cos(fineRad) + Math.sin(fineRad) * ratio + (fine !== 0 ? 0.02 : 0);
    const sx = (_camTransform.flip_h ? -1 : 1) * autoScale;
    const sy = (_camTransform.flip_v ? -1 : 1) * autoScale;
    ctx.rotate(totalRad);
    ctx.scale(sx, sy);
    ctx.drawImage(vid, -w / 2, -h / 2, w, h);
    ctx.restore();
  }

  function _captureTimelapseFrame() {
    const vid = document.getElementById('camVideo');
    if (!_tlRecorder || _tlRecorder.state === 'inactive' || !vid || !_tlCtx) return;
    if (_tlRecorder.state === 'paused') {
      try { _tlRecorder.resume(); } catch (e) {}
    }
    _drawLeveledFrame(_tlCtx, vid, _tlCanvas.width, _tlCanvas.height);
    _tlFrames += 1;
    _tlLastCaptureTs = Date.now();
    document.getElementById('tlStatus').textContent = t('tl_recording').replace('{n}', _tlFrames);
    setTimeout(function() {
      if (_tlRecorder && _tlRecorder.state === 'recording') {
        try { _tlRecorder.pause(); } catch (e) {}
      }
    }, 90);
  }

  function _frameMotionScore(vid) {
    if (!_tlProbeCanvas) {
      _tlProbeCanvas = document.createElement('canvas');
      _tlProbeCanvas.width = 64;
      _tlProbeCanvas.height = 48;
      _tlProbeCtx = _tlProbeCanvas.getContext('2d', { willReadFrequently: true });
    }
    _tlProbeCtx.drawImage(vid, 0, 0, 64, 48);
    const cur = _tlProbeCtx.getImageData(0, 0, 64, 48).data;
    if (!_tlPrevFrame) {
      _tlPrevFrame = new Uint8ClampedArray(cur);
      return 999;
    }
    let diff = 0;
    for (let i = 0; i < cur.length; i += 4) {
      diff += Math.abs(cur[i] - _tlPrevFrame[i]) + Math.abs(cur[i + 1] - _tlPrevFrame[i + 1]);
    }
    _tlPrevFrame.set(cur);
    return diff / (64 * 48 * 2);
  }

  function startTimelapse() {
    const vid = document.getElementById('camVideo');
    if (!vid || !vid.videoWidth || !vid.videoHeight) {
      document.getElementById('tlStatus').textContent = t('tl_wait_video');
      return;
    }
    if (typeof MediaRecorder === 'undefined') {
      document.getElementById('tlStatus').textContent = t('tl_no_recorder');
      return;
    }
    const w = vid.videoWidth;
    const h = vid.videoHeight;
    _tlCanvas = document.createElement('canvas');
    _tlCanvas.width = w;
    _tlCanvas.height = h;
    _tlCtx = _tlCanvas.getContext('2d');
    const stream = _tlCanvas.captureStream(15);

    const candidates = ['video/mp4', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
    _tlMime = candidates.find(function(m) { return MediaRecorder.isTypeSupported(m); }) || '';
    _tlChunks = [];
    _tlFrames = 0;
    _tlLastLayer = -1;
    _tlPrevFrame = null;
    _tlSawMotion = true;
    _tlStillTicks = 0;
    _tlLastCaptureTs = 0;
    _tlMode = document.getElementById('tlInterval').value || '5000';

    _tlRecorder = _tlMime ? new MediaRecorder(stream, { mimeType: _tlMime }) : new MediaRecorder(stream);
    _tlRecorder.ondataavailable = function(e) { if (e.data && e.data.size > 0) _tlChunks.push(e.data); };
    _tlRecorder.onstop = function() {
      if (!_tlChunks.length) {
        document.getElementById('tlStatus').textContent = t('tl_no_frames');
        return;
      }
      const blob = new Blob(_tlChunks, { type: _tlMime || 'video/mp4' });
      const ext = (_tlMime && _tlMime.indexOf('mp4') !== -1) ? 'mp4' : 'webm';
      document.getElementById('tlStatus').textContent = t('tl_saving').replace('{n}', _tlFrames);
      const reader = new FileReader();
      reader.onloadend = function() {
        const res = String(reader.result || '');
        const comma = res.indexOf(',');
        const b64 = comma !== -1 ? res.slice(comma + 1) : '';
        if (!b64) {
          document.getElementById('tlStatus').textContent = t('tl_encode_err');
          return;
        }
        send({ action: 'save_timelapse', ext: ext, frames: _tlFrames, data_b64: b64 });
      };
      reader.readAsDataURL(blob);
    };
    _tlRecorder.start();

    if (_tlMode === 'layer') {
      _captureTimelapseFrame();
      document.getElementById('tlStatus').textContent = t('tl_layer_mode').replace('{n}', _tlFrames);
    } else if (_tlMode === 'park') {
      document.getElementById('tlStatus').textContent = t('tl_park_wait');
      _tlTimer = setInterval(function() {
        if (!_tlRecorder || _tlRecorder.state === 'inactive') return;
        const motion = _frameMotionScore(vid);
        if (motion > 4.5) {
          _tlSawMotion = true;
          _tlStillTicks = 0;
        } else if (motion < 2.2) {
          _tlStillTicks += 1;
          if (_tlSawMotion && _tlStillTicks >= 2 && (Date.now() - _tlLastCaptureTs) > 2500) {
            _tlSawMotion = false;
            _captureTimelapseFrame();
          }
        }
      }, 350);
    } else {
      const intervalMs = parseInt(_tlMode, 10) || 5000;
      _captureTimelapseFrame();
      _tlTimer = setInterval(_captureTimelapseFrame, intervalMs);
    }
    _tlManualStopped = false;
    document.getElementById('btnTlStart').style.display = 'none';
    document.getElementById('btnTlStop').style.display = '';
    document.getElementById('tlInterval').disabled = true;
  }

  function stopTimelapse(byUser) {
    if (byUser) _tlManualStopped = true;
    if (_tlTimer) { clearInterval(_tlTimer); _tlTimer = null; }
    if (_tlRecorder && _tlRecorder.state !== 'inactive') {
      try {
        if (_tlRecorder.state === 'paused') _tlRecorder.resume();
        _tlRecorder.stop();
      } catch (e) {}
    }
    _tlRecorder = null;
    document.getElementById('btnTlStart').style.display = '';
    document.getElementById('btnTlStop').style.display = 'none';
    document.getElementById('tlInterval').disabled = false;
  }

  let _tlAutoEnabled = false;
  let _tlManualStopped = false;
  let _tlPrevPrinterStatus = '';
  function toggleTimelapseAuto(checked) {
    _tlAutoEnabled = !!checked;
    send({action: 'set_timelapse_auto', enabled: _tlAutoEnabled});
  }
  function saveTimelapseDir() {
    const val = (document.getElementById('tlDirInput').value || '').trim();
    send({action: 'set_timelapse_dir', path: val});
  }
  function stopCamera(silent) {
    stopTimelapse();
    _camRetryTimers.forEach(function(t) { clearTimeout(t); });
    _camRetryTimers = [];
    if (_camWs) { try { _camWs.close(); } catch (e) {} _camWs = null; }
    if (_camPc) { try { _camPc.close(); } catch (e) {} _camPc = null; }
    const vid = document.getElementById('camVideo');
    if (vid) vid.srcObject = null;
    setCamUi(false, '');
    if (!silent) send({action: 'camera_close'});
  }
  async function connectKvsWebRtc(cfg) {
    stopCamera(true);
    setCamUi(true, t('cam_ws_connecting').replace('{region}', cfg.region || ''));
    let gotAnswer = false;
    try {
      const pc = new RTCPeerConnection({ iceServers: cfg.ice_servers || [] });
      _camPc = pc;
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
      pc.ontrack = function(ev) {
        const vid = document.getElementById('camVideo');
        if (vid && ev.streams && ev.streams[0]) {
          vid.muted = true;
          vid.playsInline = true;
          vid.srcObject = ev.streams[0];
          vid.onloadedmetadata = function() {
            vid.play().catch(function(){});
          };
          vid.play().catch(function(){});
          _camReconnectCount = 0;
          setCamUi(true, t('cam_active').replace('{region}', cfg.region || 'AWS KVS'));
        }
      };
      pc.oniceconnectionstatechange = function() {
        const st = pc.iceConnectionState;
        if (st === 'checking') {
          setCamUi(true, t('cam_ice_checking'));
        } else if (st === 'failed' || st === 'disconnected') {
          if (_camReconnectCount < 2) {
            _camReconnectCount += 1;
            setCamUi(true, t('cam_dropped_retry').replace('{st}', st).replace('{n}', _camReconnectCount));
            _camRetryTimers.push(setTimeout(function() { startCamera(true); }, 2500));
          } else {
            setCamUi(true, t('cam_dropped_final').replace('{st}', st));
          }
        }
      };
      const ws = new WebSocket(cfg.wss_url);
      _camWs = ws;
      pc.onicecandidate = function(ev) {
        if (ev.candidate && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({
            action: 'ICE_CANDIDATE',
            messagePayload: btoa(JSON.stringify(ev.candidate))
          }));
        }
      };
      function sendOffer(offerPayload, attempt) {
        if (gotAnswer || ws.readyState !== WebSocket.OPEN) return;
        setCamUi(true, t('cam_waiting_printer').replace('{n}', attempt));
        ws.send(JSON.stringify({
          action: 'SDP_OFFER',
          messagePayload: offerPayload
        }));
      }
      ws.onopen = async function() {
        setCamUi(true, t('cam_ws_open'));
        const offer = await pc.createOffer({ offerToReceiveVideo: true, offerToReceiveAudio: false });
        await pc.setLocalDescription(offer);
        const encoded = btoa(JSON.stringify(pc.localDescription));
        sendOffer(encoded, 1);
        _camRetryTimers.push(setTimeout(function() { sendOffer(encoded, 2); }, 2500));
        _camRetryTimers.push(setTimeout(function() { sendOffer(encoded, 3); }, 6000));
      };
      ws.onmessage = async function(ev) {
        if (!ev.data) return;
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (!msg.messagePayload) return;
        let payload;
        try { payload = JSON.parse(atob(msg.messagePayload)); } catch (e) { return; }
        if (msg.messageType === 'SDP_ANSWER') {
          gotAnswer = true;
          _camRetryTimers.forEach(function(t) { clearTimeout(t); });
          _camRetryTimers = [];
          setCamUi(true, t('cam_sdp_answer'));
          await pc.setRemoteDescription(new RTCSessionDescription(payload));
        } else if (msg.messageType === 'ICE_CANDIDATE') {
          await pc.addIceCandidate(new RTCIceCandidate(payload));
        }
      };
      ws.onclose = function(ev) {
        if (!gotAnswer) {
          setCamUi(true, t('cam_ws_closed').replace('{code}', ev.code).replace('{reason}', ev.reason ? (': ' + ev.reason) : ''));
        }
      };
      ws.onerror = function() {
        if (!gotAnswer) {
          setCamUi(true, t('cam_ws_error'));
        }
      };
    } catch (err) {
      setCamUi(true, t('cam_webrtc_error') + err);
    }
  }
  function formatEta(remainMin) {
    if (!remainMin || remainMin <= 0) return '—';
    const eta = new Date(Date.now() + remainMin * 60000);
    const hh = String(eta.getHours()).padStart(2, '0');
    const mm = String(eta.getMinutes()).padStart(2, '0');
    return remainMin + ' ' + t('min_short') + ' (' + hh + ':' + mm + ')';
  }
  let _uploadState = null;
  function effectivePrinterStatus(p) {
    if (!p || !p.online) return 'offline';
    if (p.status === 'downloading') return 'downloading';
    const hasLiveTemps = (p.nozzle_temp !== null && p.nozzle_temp !== undefined) ||
      (p.bed_temp !== null && p.bed_temp !== undefined);
    if (
      p.status === 'printing' &&
      hasLiveTemps &&
      (p.current_layer || 0) === 0 &&
      (p.total_layers || 0) === 0 &&
      (p.remaining_time_min || 0) === 0 &&
      (p.target_nozzle_temp || 0) <= 0 &&
      (p.target_bed_temp || 0) <= 0
    ) {
      return 'downloading';
    }
    return p.status || 'idle';
  }
  function renderMonitor(printers, selectedId) {
    const card = document.getElementById('monitorCard');
    const p = printers.find(function(x) { return x.printer_id === selectedId; }) || printers[0];
    if (!p) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    document.getElementById('monitorTitle').textContent = p.name;
    const statusMap = {
      idle: t('status_idle'),
      downloading: t('status_downloading'),
      printing: t('status_printing'),
      paused: t('status_paused'),
      offline: t('status_offline')
    };
    const effStatus = (_uploadState && _uploadState.active) ? 'downloading' : effectivePrinterStatus(p);
    const badge = document.getElementById('monitorBadge');
    badge.className = 'badge ' + effStatus;
    if (_uploadState && _uploadState.active) {
      badge.textContent = t('status_downloading') + ' ' + (_uploadState.pct || 0) + '%';
    } else if (effStatus === 'downloading') {
      badge.textContent = (p.progress || 0) > 0 ? (t('status_downloading') + ' ' + p.progress + '%') : t('status_downloading');
    } else {
      badge.textContent = statusMap[effStatus] || effStatus;
    }
    document.getElementById('monitorFile').textContent = (_uploadState && _uploadState.active && _uploadState.text)
      ? _uploadState.text
      : (p.current_file
          ? (t('file_prefix') + p.current_file)
          : (p.firmware_version ? (t('fw_prefix') + p.firmware_version) : t('no_active_job')));
    const pct = (_uploadState && _uploadState.active)
      ? (_uploadState.pct || 0)
      : ((effStatus === 'printing' || effStatus === 'paused' || effStatus === 'downloading') ? (p.progress || 0) : 0);
    document.getElementById('monitorProgressBar').style.width = pct + '%';
    document.getElementById('statProgress').textContent = pct + '%';
    document.getElementById('statLayers').textContent = p.total_layers > 0
      ? (p.current_layer + ' / ' + p.total_layers)
      : '—';
    document.getElementById('statRemain').textContent = (effStatus === 'printing' || effStatus === 'paused')
      ? formatEta(p.remaining_time_min)
      : '—';
    const nt = p.nozzle_temp !== null && p.nozzle_temp !== undefined ? Math.round(p.nozzle_temp) : null;
    const tnt = p.target_nozzle_temp !== null && p.target_nozzle_temp !== undefined ? Math.round(p.target_nozzle_temp) : null;
    document.getElementById('statNozzle').textContent = nt !== null
      ? (tnt !== null ? (nt + ' / ' + tnt + ' °C') : (nt + ' °C'))
      : '—';
    const bt = p.bed_temp !== null && p.bed_temp !== undefined ? Math.round(p.bed_temp) : null;
    const tbt = p.target_bed_temp !== null && p.target_bed_temp !== undefined ? Math.round(p.target_bed_temp) : null;
    document.getElementById('statBed').textContent = bt !== null
      ? (tbt !== null ? (bt + ' / ' + tbt + ' °C') : (bt + ' °C'))
      : '—';

    const activeJob = p.status === 'printing' || p.status === 'paused';
    document.getElementById('btnPause').style.display = p.status === 'printing' ? '' : 'none';
    document.getElementById('btnResume').style.display = p.status === 'paused' ? '' : 'none';
    document.getElementById('btnStop').disabled = !activeJob;
    document.getElementById('speedSelect').disabled = effStatus !== 'printing';
    const wasActive = (_tlPrevPrinterStatus === 'printing' || _tlPrevPrinterStatus === 'paused');
    _tlPrevPrinterStatus = effStatus;

    if (_tlRecorder && _tlMode === 'layer' && effStatus === 'printing' && p.current_layer > 0) {
      if (_tlLastLayer !== p.current_layer) {
        _tlLastLayer = p.current_layer;
        _captureTimelapseFrame();
      }
    }

    if (effStatus === 'idle') {
      _tlManualStopped = false;
    }

    // Auto-start timelapse when print starts (unless user manually stopped it for this print)
    if (_tlAutoEnabled && !_tlRecorder && !_tlManualStopped && effStatus === 'printing') {
      const vid = document.getElementById('camVideo');
      if (vid && vid.videoWidth > 0 && vid.videoHeight > 0) {
        startTimelapse();
      }
    }

    // Auto-save timelapse when print finishes (printing/paused -> idle)
    if (_tlRecorder && wasActive && effStatus === 'idle') {
      stopTimelapse();
    }
  }
  function saveManualToken() {
    const val = document.getElementById('manualToken').value.trim();
    if (!val) { showBanner('err', t('enter_token')); return; }
    document.getElementById('manualToken').value = '';
    send({action: 'set_token', token: val});
  }
  let _lastState = null;
  function applyPrinterFilter() {
    if (!_lastState) return;
    renderPrintersList(_lastState);
  }
  function renderPrintersList(st) {
    const listEl = document.getElementById('printersList');
    const printers = st.printers || [];
    if (!printers.length) {
      listEl.textContent = st.config && st.config.has_token
        ? t('no_printers')
        : t('login_to_see_printers');
      return;
    }
    const q = (document.getElementById('printerSearch').value || '').trim().toLowerCase();
    const flt = document.getElementById('printerStatusFilter').value || 'all';
    const filtered = printers.filter(function(p) {
      if (flt === 'idle' && p.status !== 'idle') return false;
      if (flt === 'printing' && p.status !== 'printing' && p.status !== 'paused') return false;
      if (flt === 'online' && !p.online) return false;
      if (q) {
        const hay = (p.name + ' ' + (p.model || '') + ' ' + p.printer_id).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    if (!filtered.length) {
      listEl.textContent = t('no_printers_filter');
      return;
    }
    listEl.innerHTML = '';
    filtered.forEach(function(p) {
      const div = document.createElement('div');
      const selected = st.config && st.config.printer_id === p.printer_id;
      div.className = 'printer-item' + (selected ? ' selected' : '');
      div.onclick = function() {
        send({action: 'select_printer', printer_id: p.printer_id, printer_name: p.name});
      };
      const effStatus = effectivePrinterStatus(p);
      const statusMap = {
        idle: t('status_idle'),
        downloading: (p.progress || 0) > 0 ? (t('status_downloading') + ' ' + p.progress + '%') : t('status_downloading'),
        printing: t('status_printing') + ' ' + p.progress + '%',
        paused: t('status_paused'),
        offline: t('status_offline')
      };
      const badgeCls = effStatus;
      const tempParts = [];
      if (p.nozzle_temp !== null && p.nozzle_temp !== undefined) tempParts.push(t('nozzle_short') + ' ' + Math.round(p.nozzle_temp) + '°C');
      if (p.bed_temp !== null && p.bed_temp !== undefined) tempParts.push(t('bed_short') + ' ' + Math.round(p.bed_temp) + '°C');
      const meta = [p.model || ('ID ' + p.printer_id), p.current_file, tempParts.join(' · ')].filter(Boolean).join(' · ');
      div.innerHTML =
        '<div><strong>' + escapeHtml(p.name) + '</strong>' +
        '<div class="sub">' + escapeHtml(meta) + '</div></div>' +
        '<span class="badge ' + badgeCls + '">' + (statusMap[effStatus] || effStatus) + '</span>';
      listEl.appendChild(div);
    });
  }
  function renderState(st) {
    _lastState = st;
    if (st.config && st.config.language) {
      applyLanguage(st.config.language);
    }
    const btnRef = document.getElementById('btnRefresh');
    if (btnRef) { btnRef.disabled = false; btnRef.textContent = t('btn_refresh'); }
    const btnFiles = document.getElementById('btnRefreshFiles');
    if (btnFiles) { btnFiles.disabled = false; btnFiles.textContent = t('btn_refresh_files'); }
    const updEl = document.getElementById('lastUpdated');
    if (updEl && st.updated_at) { updEl.textContent = t('updated_prefix') + st.updated_at; }
    const spaceEl = document.getElementById('cloudSpace');
    if (spaceEl) { spaceEl.textContent = (st.cloud_storage && st.cloud_storage.label) ? (t('used_prefix') + st.cloud_storage.label) : ''; }
    const tlDirEl = document.getElementById('tlDirInput');
    if (tlDirEl && document.activeElement !== tlDirEl) {
      tlDirEl.value = (st.config && st.config.timelapse_dir) ? st.config.timelapse_dir : (st.default_timelapse_dir || '');
    }
    const tlAutoEl = document.getElementById('tlAutoCheckbox');
    if (tlAutoEl && st.config) {
      _tlAutoEnabled = !!st.config.timelapse_auto;
      tlAutoEl.checked = _tlAutoEnabled;
    }
    const authEl = document.getElementById('authStatus');
    const hasHelper = !!st.has_login_helper;
    const hasToken = !!(st.config && st.config.has_token);
    document.getElementById('btnLogin').style.display = (hasHelper && !hasToken) ? '' : 'none';
    document.getElementById('btnRelogin').style.display = (hasHelper && hasToken) ? '' : 'none';
    document.getElementById('btnLogout').style.display = hasToken ? '' : 'none';
    if (!hasHelper && !hasToken) {
      document.getElementById('manualTokenBox').open = true;
    }
    if (hasToken) {
      const userLabel = st.user && st.user.email ? ' (' + st.user.email + ')' : '';
      authEl.textContent = t('signed_in') + userLabel;
    } else {
      authEl.textContent = hasHelper
        ? t('not_signed_in_helper')
        : t('not_signed_in_manual');
    }
    const printers = st.printers || [];
    const selId = st.config ? st.config.printer_id : null;
    if (selId && st.config && st.config.camera_transforms && st.config.camera_transforms[String(selId)]) {
      const saved = st.config.camera_transforms[String(selId)];
      _camTransform = {
        angle: Number(saved.angle || 0),
        base_rot: Number(saved.base_rot || 0),
        flip_h: !!saved.flip_h,
        flip_v: !!saved.flip_v
      };
      applyCamTransformCss();
    }
    renderMonitor(printers, selId);
    renderPrintersList(st);
    const filesEl = document.getElementById('cloudFilesList');
    const files = st.cloud_files || [];
    if (!files.length) {
      filesEl.textContent = st.config && st.config.has_token
        ? t('no_files')
        : t('login_to_see_files');
    } else {
      filesEl.innerHTML = '';
      files.forEach(function(f) {
        const div = document.createElement('div');
        div.className = 'printer-item';
        div.style.cursor = 'default';
        const meta = [f.size_mb + ' MB', f.created_at].filter(Boolean).join(' · ');
        const left = document.createElement('div');
        left.style.flex = '1';
        left.style.minWidth = '0';
        left.style.marginRight = '10px';
        left.innerHTML = '<div style="font-weight:600;word-break:break-word;">' + escapeHtml(f.name) + '</div><div class="sub">' + escapeHtml(meta) + '</div>';
        const actions = document.createElement('div');
        actions.className = 'row';
        actions.style.flexShrink = '0';
        const btnPrint = document.createElement('button');
        btnPrint.textContent = t('btn_print');
        btnPrint.onclick = function() { printCloudFile(f.file_id, f.gcode_id, f.name, f.url); };
        const btnDel = document.createElement('button');
        btnDel.className = 'secondary';
        btnDel.textContent = t('btn_delete');
        btnDel.onclick = function() { deleteCloudFile(f.file_id, f.name); };
        actions.appendChild(btnPrint);
        actions.appendChild(btnDel);
        div.appendChild(left);
        div.appendChild(actions);
        filesEl.appendChild(div);
      });
    }
    if (st.error) showBanner('err', st.error);
    else if (st.notice) showBanner('ok', st.notice);
    else if (st.info) showBanner('info', st.info);
    else showBanner('', '');
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function(c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  if (window.orca && typeof window.orca.onMessage === 'function') {
    window.orca.onMessage(function(msg) {
      if (msg && msg.type === 'state') renderState(msg);
      else if (msg && msg.type === 'camera_session') connectKvsWebRtc(msg.session);
      else if (msg && msg.type === 'camera_error') setCamUi(true, msg.error || t('cam_failed'));
      else if (msg && msg.type === 'upload_progress') {
        _uploadState = msg.active ? { active: true, pct: msg.pct || 0, text: msg.text || '' } : null;
        if (_lastState) renderMonitor(_lastState.printers || [], _lastState.config && _lastState.config.printer_id);
      }
      else if (msg && msg.type === 'timelapse_saved') {
        document.getElementById('tlStatus').textContent = msg.text || t('tl_saved_default');
        showBanner('ok', msg.text || t('tl_saved_default'));
      }
    });
  }
  window.addEventListener('DOMContentLoaded', function() {
    applyLanguage(_lang);
    send({action: 'init'});
    setInterval(function() {
      if (document.visibilityState === 'visible') {
        send({action: 'poll_status'});
      }
    }, 10000);
  });
</script>
</body>
</html>
"""


def _decode_validated_video(data_b64: str, ext: str) -> bytes:
    """Decode base64-encoded timelapse video and verify MP4/WebM container magic bytes."""
    raw = base64.b64decode(data_b64, validate=True)
    if len(raw) < 12:
        raise ValueError("Empty or truncated video data.")
    if ext == "mp4" and b"ftyp" not in raw[:16]:
        raise ValueError("Invalid MP4 video header.")
    if ext == "webm" and not raw.startswith(b"\x1a\x45\xdf\xa3"):
        raise ValueError("Invalid WebM video header.")
    return raw


class AnycubicCloudPage(orca.pages.PagesPluginCapabilityBase):
    """In-slicer page for Anycubic Cloud login, printer selection, and status."""

    def __init__(self) -> None:
        global _ACTIVE_PAGE
        super().__init__()
        self._printers: List[Dict[str, Any]] = []
        self._user: Dict[str, Any] = {}
        self._updated_at: str = ""
        self._cloud_files: List[Dict[str, Any]] = []
        self._cloud_storage: Dict[str, Any] = {}
        self._mqtt_live: Dict[int, Dict[str, Any]] = {}
        self._mqtt = AnycubicMqttWatcher(on_telemetry=self._on_mqtt_telemetry)
        _ACTIVE_PAGE = self

    def _on_mqtt_telemetry(self, printer_id: int, patch: Dict[str, Any]) -> None:
        live = self._mqtt_live.setdefault(printer_id, {})
        for k in ("target_nozzle_temp", "target_bed_temp"):
            if k in patch:
                live[k] = patch[k]
        if patch.get("status") == "idle":
            self._mqtt_live.pop(printer_id, None)
        updated = False
        for item in self._printers:
            if item.get("printer_id") == printer_id:
                item.update(patch)
                updated = True
                break
        if updated:
            self._updated_at = datetime.datetime.now().strftime("%H:%M:%S")
            self._emit_state()

    def trigger_files_refresh(self, notice: str = "") -> None:
        cfg = load_config(path=_CONFIG_FILE)
        if not cfg.token:
            return

        def _do() -> None:
            try:
                client = AnycubicCloudClient(token=cfg.token)
                self._cloud_storage = client.get_cloud_storage()
                self._cloud_files = [f.to_dict() for f in client.get_cloud_files(limit=25)]
                self._emit_state(cfg=cfg, notice=notice)
            except AnycubicError:
                pass

        self._run_bg(_do)

    def get_name(self) -> str:
        return "OrcaCubic"

    def get_ui(self) -> str:
        return _PAGE_HTML

    def get_icon(self) -> str:
        return ""

    def _emit_state(
        self,
        cfg: Optional[OrcaCubicConfig] = None,
        notice: str = "",
        error: str = "",
        info: str = "",
    ) -> None:
        current_cfg = cfg if cfg is not None else load_config(path=_CONFIG_FILE)
        self.post_message(
            {
                "type": "state",
                "config": current_cfg.sanitized_dict(),
                "user": self._user,
                "printers": self._printers,
                "updated_at": self._updated_at,
                "cloud_files": self._cloud_files,
                "cloud_storage": self._cloud_storage,
                "default_timelapse_dir": str(_TIMELAPSE_DIR),
                "has_login_helper": (
                find_login_helper(extra_search_dirs=[_PLUGIN_DIR]) is not None
                or find_chromium_browser() is not None
            ),
                "notice": notice,
                "error": error,
                "info": info,
            }
        )

    def _refresh_printers_sync(self, cfg: OrcaCubicConfig, notice: str = "") -> None:
        if not cfg.token:
            self._printers = []
            self._user = {}
            self._emit_state(cfg=cfg, notice=notice)
            return
        try:
            client = AnycubicCloudClient(token=cfg.token)
            self._user = client.get_user_info()
            printers = client.get_printers_with_live_status()
            self._printers = [p.to_dict() for p in printers]
            for item in self._printers:
                pid = item.get("printer_id")
                if item.get("status") in ("idle", "offline"):
                    self._mqtt_live.pop(pid, None)
                elif pid in self._mqtt_live:
                    for k, v in self._mqtt_live[pid].items():
                        if item.get(k) is None:
                            item[k] = v
            self._updated_at = datetime.datetime.now().strftime("%H:%M:%S")
            mqtt_targets = [
                (p.printer_id, p.machine_type, p.printer_key)
                for p in printers
                if p.machine_type and p.printer_key
            ]
            self._mqtt.configure(
                token=cfg.token,
                email=str(self._user.get("email") or ""),
                targets=mqtt_targets,
            )
            if _ACTIVE_AGENT is not None and printers:
                _ACTIVE_AGENT.announce_printers(printers)
            if cfg.printer_id is None and printers:
                chosen = _resolve_target_printer(client, cfg)
                cfg.printer_id = chosen.printer_id
                cfg.printer_name = chosen.name
                save_config(cfg, path=_CONFIG_FILE)
            try:
                self._cloud_storage = client.get_cloud_storage()
                self._cloud_files = [f.to_dict() for f in client.get_cloud_files(limit=25)]
            except AnycubicError:
                pass
            self._emit_state(cfg=cfg, notice=notice)
        except AnycubicTokenExpiredError as exc:
            self._printers = []
            self._user = {}
            self._emit_state(cfg=cfg, error=str(exc))
        except AnycubicError as exc:
            self._emit_state(cfg=cfg, error=str(exc))

    def _run_bg(self, fn: Any) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def on_message(self, message: Any) -> None:
        try:
            data = json.loads(message) if isinstance(message, str) else message
        except ValueError:
            return
        if not isinstance(data, dict):
            return

        action = str(data.get("action") or "")

        if action == "init":
            cfg = load_config(path=_CONFIG_FILE)
            self._emit_state(cfg=cfg)
            if cfg.token:
                self._run_bg(lambda: self._refresh_printers_sync(cfg))
            return

        if action == "login_window":
            reset = bool(data.get("reset_session", False))
            self._emit_state(info="Открыто окно входа Anycubic Cloud...")

            def _do_login() -> None:
                try:
                    token = launch_login_window(
                        reset_session=reset,
                        extra_search_dirs=[_PLUGIN_DIR],
                        profile_dir=_DATA_DIR / "browser_login_profile",
                    )
                    cfg = load_config(path=_CONFIG_FILE)
                    cfg.token = token
                    save_config(cfg, path=_CONFIG_FILE)
                    self._refresh_printers_sync(
                        cfg, notice="Вход в Anycubic Cloud выполнен успешно!"
                    )
                except AnycubicError as exc:
                    self._emit_state(error=str(exc))

            self._run_bg(_do_login)
            return

        if action == "set_token":
            token = str(data.get("token") or "").strip()
            cfg = load_config(path=_CONFIG_FILE)
            cfg.token = token
            save_config(cfg, path=_CONFIG_FILE)
            self._emit_state(cfg=cfg, info="Проверка токена...")
            self._run_bg(
                lambda: self._refresh_printers_sync(cfg, notice="Токен сохранён и проверен.")
            )
            return

        if action == "logout":
            cfg = load_config(path=_CONFIG_FILE)
            cfg.token = ""
            save_config(cfg, path=_CONFIG_FILE)
            self._printers = []
            self._user = {}
            self._cloud_files = []
            self._cloud_storage = {}
            self._emit_state(cfg=cfg, notice="Вы вышли из аккаунта Anycubic Cloud.")
            return

        if action == "refresh_files":
            cfg = load_config(path=_CONFIG_FILE)
            if not cfg.token:
                self._emit_state(cfg=cfg, error="Сначала войдите в Anycubic Cloud.")
                return
            self._emit_state(cfg=cfg, info="Загрузка списка облачных файлов...")

            def _do_files() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    self._cloud_storage = client.get_cloud_storage()
                    self._cloud_files = [f.to_dict() for f in client.get_cloud_files(limit=25)]
                    self._emit_state(cfg=cfg, notice="Список облачных файлов обновлён.")
                except AnycubicError as exc:
                    self._emit_state(cfg=cfg, error=str(exc))

            self._run_bg(_do_files)
            return

        if action == "print_cloud_file":
            cfg = load_config(path=_CONFIG_FILE)
            fid = int(data.get("file_id") or 0)
            gid = int(data.get("gcode_id") or 0)
            fname = str(data.get("name") or f"#{fid}")
            furl = str(data.get("url") or "")
            if not cfg.token or fid <= 0:
                return
            self._emit_state(cfg=cfg, info=f"Отправка «{fname}» на печать...")

            def _do_print() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    printer = _resolve_target_printer(client, cfg)
                    client.start_print_cloud_file(
                        printer_id=printer.printer_id,
                        file_id=fid,
                        gcode_id=gid,
                        file_url=furl,
                        file_name=fname,
                    )
                    self._emit_state(
                        cfg=cfg,
                        notice=f"Печать «{fname}» запущена на {printer.name}!",
                    )
                except Exception as exc:
                    print(f"[OrcaCubic] Cloud print failed: {exc!r}", file=sys.stderr, flush=True)
                    self._emit_state(cfg=cfg, error=str(exc))

            self._run_bg(_do_print)
            return

        if action == "delete_cloud_file":
            cfg = load_config(path=_CONFIG_FILE)
            fid = int(data.get("file_id") or 0)
            fname = str(data.get("name") or f"#{fid}")
            if not cfg.token or fid <= 0:
                return
            self._emit_state(cfg=cfg, info=f"Удаление «{fname}»...")

            def _do_delete() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    client.delete_cloud_file(fid)
                    self._cloud_storage = client.get_cloud_storage()
                    self._cloud_files = [f.to_dict() for f in client.get_cloud_files(limit=25)]
                    self._emit_state(cfg=cfg, notice=f"Файл «{fname}» удалён из облака.")
                except AnycubicError as exc:
                    self._emit_state(cfg=cfg, error=str(exc))

            self._run_bg(_do_delete)
            return

        if action == "refresh_printers":
            cfg = load_config(path=_CONFIG_FILE)
            self._emit_state(cfg=cfg, info="Обновление статуса принтеров...")
            self._run_bg(lambda: self._refresh_printers_sync(cfg, notice="Статус обновлён."))
            return

        if action == "poll_status":
            cfg = load_config(path=_CONFIG_FILE)
            if not cfg.token:
                return

            def _do_poll() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    printers = client.get_printers_with_live_status()
                    self._printers = [p.to_dict() for p in printers]
                    self._updated_at = datetime.datetime.now().strftime("%H:%M:%S")
                    self._emit_state(cfg=cfg)
                except AnycubicError:
                    pass

            self._run_bg(_do_poll)
            return

        if action == "printer_control":
            cfg = load_config(path=_CONFIG_FILE)
            cmd = str(data.get("cmd") or "")
            param = data.get("param")
            if not cfg.token or cmd not in ("pause", "resume", "stop", "print_speed"):
                return
            self._emit_state(cfg=cfg, info="Отправка команды принтеру...")

            def _do_ctrl() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    printer = _resolve_target_printer(client, cfg)
                    pid = printer.printer_id
                    if cmd == "pause":
                        client.pause_print(printer_id=pid)
                        msg = f"Пауза отправлена на {printer.name}."
                    elif cmd == "resume":
                        client.resume_print(printer_id=pid)
                        msg = f"Печать продолжена на {printer.name}."
                    elif cmd == "stop":
                        client.stop_print(printer_id=pid)
                        msg = f"Печать остановлена на {printer.name}."
                    else:
                        speed_mode = max(1, min(3, int(str(param))))
                        client.update_print_settings(
                            printer_id=pid, settings={"print_speed_mode": speed_mode}
                        )
                        msg = f"Скорость печати обновлена на {printer.name}."
                    self._refresh_printers_sync(cfg, notice=msg)
                except AnycubicError as exc:
                    self._emit_state(cfg=cfg, error=str(exc))

            self._run_bg(_do_ctrl)
            return

        if action == "camera_open":
            cfg = load_config(path=_CONFIG_FILE)
            if not cfg.token:
                self.post_message({"type": "camera_error", "error": "Сначала войдите в Anycubic Cloud."})
                return

            def _do_cam_open() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    printer = _resolve_target_printer(client, cfg)
                    session = open_camera_session(client, printer.printer_id)
                    self.post_message({"type": "camera_session", "session": session})
                except AnycubicError as exc:
                    self.post_message({"type": "camera_error", "error": str(exc)})
                except Exception as exc:
                    self.post_message({"type": "camera_error", "error": f"Ошибка камеры: {exc}"})

            self._run_bg(_do_cam_open)
            return

        if action == "camera_close":
            cfg = load_config(path=_CONFIG_FILE)
            if not cfg.token:
                return

            def _do_cam_close() -> None:
                try:
                    client = AnycubicCloudClient(token=cfg.token)
                    printer = _resolve_target_printer(client, cfg)
                    close_camera_session(client, printer.printer_id)
                except Exception:
                    pass

            self._run_bg(_do_cam_close)
            return

        if action == "save_camera_transform":
            cfg = load_config(path=_CONFIG_FILE)
            tf = data.get("transform")
            if cfg.printer_id is not None and isinstance(tf, dict):
                cfg.camera_transforms[str(cfg.printer_id)] = {
                    "angle": float(tf.get("angle") or 0.0),
                    "base_rot": int(tf.get("base_rot") or 0) % 360,
                    "flip_h": bool(tf.get("flip_h", False)),
                    "flip_v": bool(tf.get("flip_v", False)),
                }
                save_config(cfg, path=_CONFIG_FILE)
            return

        if action == "browse_timelapse_dir":
            def _do_pick() -> None:
                try:
                    picked = pick_folder_dialog(extra_search_dirs=[_PLUGIN_DIR])
                    if picked:
                        cfg = load_config(path=_CONFIG_FILE)
                        cfg.timelapse_dir = picked
                        save_config(cfg, path=_CONFIG_FILE)
                        self._emit_state(cfg=cfg, notice=f"Папка таймлапсов: {picked}")
                except Exception as exc:
                    self._emit_state(error=f"Не удалось открыть проводник: {exc}")

            self._run_bg(_do_pick)
            return

        if action == "set_timelapse_auto":
            cfg = load_config(path=_CONFIG_FILE)
            cfg.timelapse_auto = bool(data.get("enabled", False))
            save_config(cfg, path=_CONFIG_FILE)
            self._emit_state(
                cfg=cfg,
                notice="Автозапуск и автосохранение таймлапса включены."
                if cfg.timelapse_auto
                else "Автозапуск таймлапса выключен.",
            )
            return

        if action == "set_timelapse_dir":
            cfg = load_config(path=_CONFIG_FILE)
            raw_path = str(data.get("path") or "").strip()
            cfg.timelapse_dir = raw_path
            save_config(cfg, path=_CONFIG_FILE)
            shown = str(Path(raw_path).expanduser()) if raw_path else str(_TIMELAPSE_DIR)
            self._emit_state(cfg=cfg, notice=f"Папка таймлапсов: {shown}")
            return

        if action == "set_language":
            lang = str(data.get("language") or "auto").lower()
            if lang in ("auto", "en", "ru"):
                cfg = load_config(path=_CONFIG_FILE)
                cfg.language = lang
                save_config(cfg, path=_CONFIG_FILE)
                self._emit_state(cfg=cfg)
            return

        if action == "save_timelapse":
            ext = "mp4" if str(data.get("ext") or "mp4").lower() == "mp4" else "webm"
            frames = int(data.get("frames") or 0)
            b64 = str(data.get("data_b64") or "")
            if not b64:
                return
            cfg = load_config(path=_CONFIG_FILE)
            target_dir = (
                Path(cfg.timelapse_dir).expanduser()
                if cfg.timelapse_dir
                else _TIMELAPSE_DIR
            )
            try:
                video_bytes = _decode_validated_video(b64, ext)
                if not target_dir.exists():
                    target_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                out_name = f"timelapse_{ts}.mp4" if ext == "mp4" else f"timelapse_{ts}.webm"
                out_path = target_dir / out_name
                with open(out_path, "wb") as fp:
                    fp.write(video_bytes)
                self.post_message(
                    {
                        "type": "timelapse_saved",
                        "text": f"Timelapse saved ({frames} frames): {out_path}",
                    }
                )
            except Exception as exc:
                self.post_message(
                    {
                        "type": "timelapse_saved",
                        "text": f"Failed to save timelapse to {target_dir}: {exc}",
                    }
                )
            return

        if action == "select_printer":
            cfg = load_config(path=_CONFIG_FILE)
            pid = data.get("printer_id")
            if pid is not None:
                cfg.printer_id = int(pid)
                cfg.printer_name = str(data.get("printer_name") or "")
                save_config(cfg, path=_CONFIG_FILE)
                self._emit_state(
                    cfg=cfg, notice=f"Выбран принтер: {cfg.printer_name or cfg.printer_id}"
                )
            return

        if action == "set_mode":
            mode = str(data.get("mode") or "print")
            if mode in ("print", "upload_only"):
                cfg = load_config(path=_CONFIG_FILE)
                cfg.default_mode = mode
                save_config(cfg, path=_CONFIG_FILE)
                label = (
                    "Отправить на печать через облако"
                    if mode == "print"
                    else "Только загрузить в облако"
                )
                self._emit_state(cfg=cfg, notice=f"Режим обновлён: {label}")
            return


@orca.plugin
class OrcaCubicPlugin(orca.base):
    """Main OrcaSlicer 2.5+ plugin entrypoint."""

    def register_capabilities(self) -> None:
        process_grants = get_required_process_grants(
            extra_search_dirs=[_PLUGIN_ROOT, _PLUGIN_DIR]
        )

        orca_data_dir = _PLUGIN_ROOT.parent.parent
        temp_dir = Path(tempfile.gettempdir())
        temp_dir_resolved = temp_dir.resolve()
        fs_read_grants = [
            str(_PLUGIN_ROOT / "*"),
            str(_PLUGIN_DIR / "*"),
            str(orca_data_dir / "*"),
            str(temp_dir / "*"),
        ]
        if temp_dir_resolved != temp_dir:
            fs_read_grants.append(str(temp_dir_resolved / "*"))
        fs_rw_grants = [str(_DATA_DIR), str(_DATA_DIR / "*")]

        orca.request_permissions(
            fs_read=fs_read_grants,
            fs_readwrite=fs_rw_grants,
            network_http=[
                "cloud-universe.anycubic.com",
                "*.amazonaws.com",
                "*.myqcloud.com",
                "*.aliyuncs.com",
                "127.0.0.1",
            ],
            network_socket=["127.0.0.1", "mqtt-universe.anycubic.com"],
            process=process_grants,
            threading=True,
        )
        orca.register_capability(AnycubicPrinterAgent)
        orca.register_capability(AnycubicCloudPage)
