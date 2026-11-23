"""Stdlib-only Anycubic Cloud REST API client for Anycubic FDM printers (Kobra 2/3/S1/X)."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .auth import (
    PUBLIC_API_ROOT,
    AnycubicAuth,
    AnycubicAuthError,
    AnycubicError,
    AnycubicTokenExpiredError,
    AnycubicUploadError,
)

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]

# Endpoints verified against WaresWichall/hass-anycubic_cloud
EP_USER_INFO = "/user/profile/userInfo"
EP_GET_PRINTERS = "/work/printer/getPrinters"
EP_PRINTER_INFO = "/v2/printer/info"
EP_GET_PROJECTS = "/work/project/getProjects"
EP_USER_STORE = "/work/index/getUserStore"
EP_USER_FILES = "/work/index/files"
EP_DELETE_CLOUD_FILE = "/work/index/delFiles"
EP_GCODE_INFO_FDM = "/work/gcode/infoFdm"
EP_LOCK_STORAGE = "/v2/cloud_storage/lockStorageSpace"
EP_UNLOCK_STORAGE = "/v2/cloud_storage/unlockStorageSpace"
EP_NEW_UPLOAD_FILE = "/v2/profile/newUploadFile"
EP_SEND_ORDER = "/work/operation/sendOrder"

ORDER_ID_START_PRINT = 1
ORDER_ID_PAUSE_PRINT = 2
ORDER_ID_RESUME_PRINT = 3
ORDER_ID_STOP_PRINT = 4
ORDER_ID_PRINT_SETTINGS = 6

# HTTP / API codes that indicate expired or invalid session token
AUTH_ERROR_CODES = {401, 403, 10001, 10002, 10003, 20001}


@dataclass
class PrinterSummary:
    printer_id: int
    name: str
    model: str
    online: bool
    status: str
    progress: int = 0
    current_file: str = ""
    raw_state: str = ""
    nozzle_temp: Optional[float] = None
    bed_temp: Optional[float] = None
    target_nozzle_temp: Optional[float] = None
    target_bed_temp: Optional[float] = None
    remaining_time_min: int = 0
    current_layer: int = 0
    total_layers: int = 0
    project_id: int = 0
    firmware_version: str = ""
    printer_key: str = ""
    machine_type: str = ""
    total_print_time: str = ""
    material_used: str = ""
    print_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "printer_id": self.printer_id,
            "name": self.name,
            "model": self.model,
            "online": self.online,
            "status": self.status,
            "progress": self.progress,
            "current_file": self.current_file,
            "raw_state": self.raw_state,
            "nozzle_temp": self.nozzle_temp,
            "bed_temp": self.bed_temp,
            "target_nozzle_temp": self.target_nozzle_temp,
            "target_bed_temp": self.target_bed_temp,
            "remaining_time_min": self.remaining_time_min,
            "current_layer": self.current_layer,
            "total_layers": self.total_layers,
            "project_id": self.project_id,
            "firmware_version": self.firmware_version,
            "printer_key": self.printer_key,
            "machine_type": self.machine_type,
            "total_print_time": self.total_print_time,
            "material_used": self.material_used,
            "print_count": self.print_count,
        }


@dataclass
class CloudFileSummary:
    file_id: int
    gcode_id: int
    name: str
    size_bytes: int
    created_at: str = ""
    url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_id": self.file_id,
            "gcode_id": self.gcode_id,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / (1024 * 1024), 2),
            "created_at": self.created_at,
            "url": self.url,
        }


def _to_int(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _normalize_printer(raw: Dict[str, Any]) -> PrinterSummary:
    pid = int(raw.get("id") or raw.get("printer_id") or 0)
    name = str(raw.get("name") or raw.get("printer_name") or f"Printer #{pid}")
    model = str(
        raw.get("model")
        or raw.get("machine_name")
        or raw.get("machine_type_name")
        or ""
    )

    device_status = raw.get("device_status")
    is_printing = raw.get("is_printing")
    online = bool(
        device_status == 1
        or str(device_status).lower() in ("1", "online", "true")
        or raw.get("online") is True
    )

    raw_state = str(raw.get("status") or raw.get("state") or "")
    progress = 0
    current_file = ""
    remaining_time_min = 0
    current_layer = 0
    total_layers = 0
    project_id = 0

    project = raw.get("project")
    proj_settings: Dict[str, Any] = {}
    if isinstance(project, dict):
        raw_settings = project.get("settings")
        if isinstance(raw_settings, dict):
            proj_settings = raw_settings
        elif isinstance(raw_settings, str) and raw_settings.strip().startswith("{"):
            try:
                parsed_s = json.loads(raw_settings)
                if isinstance(parsed_s, dict):
                    proj_settings = parsed_s
            except ValueError:
                pass
        progress = _to_int(project.get("progress"), 0)
        current_file = str(project.get("gcode_name") or project.get("name") or "")
        remaining_time_min = _to_int(
            project.get("remain_time")
            or project.get("remaining_time")
            or proj_settings.get("remain_time"),
            0,
        )
        current_layer = _to_int(
            project.get("curr_layer")
            or project.get("current_layer")
            or proj_settings.get("curr_layer")
            or proj_settings.get("current_layer"),
            0,
        )
        total_layers = _to_int(
            project.get("total_layer")
            or project.get("total_layers")
            or proj_settings.get("total_layers")
            or proj_settings.get("total_layer"),
            0,
        )
        project_id = _to_int(project.get("id") or project.get("project_id"), 0)
    else:
        progress = _to_int(raw.get("progress"), 0)
        remaining_time_min = _to_int(raw.get("remain_time"), 0)
        project_id = _to_int(raw.get("project_id"), 0)

    raw_reason = str(raw.get("reason") or "").lower()
    raw_state_l = raw_state.lower()

    if not online:
        status = "offline"
    elif (
        (not isinstance(is_printing, bool) and is_printing in (4, "4", "downloading"))
        or raw_state_l in ("downloading", "download", "receiving", "uploading")
        or raw_reason in ("downloading", "download", "receiving")
    ):
        status = "downloading"
    elif (
        is_printing is True
        or (not isinstance(is_printing, bool) and is_printing in (2, "2", "printing"))
        or raw_state_l == "printing"
    ):
        status = "printing"
    elif (
        not isinstance(is_printing, bool) and is_printing in (3, "3", "paused")
    ) or raw_state_l == "paused":
        status = "paused"
    else:
        status = "idle"

    param = raw.get("parameter") if isinstance(raw.get("parameter"), dict) else {}
    nozzle_temp = _to_float(
        raw.get("curr_nozzle_temp")
        or raw.get("nozzle_temp")
        or param.get("curr_nozzle_temp")
        or proj_settings.get("curr_nozzle_temp")
    )
    bed_temp = _to_float(
        raw.get("curr_hotbed_temp")
        or raw.get("hotbed_temp")
        or param.get("curr_hotbed_temp")
        or proj_settings.get("curr_hotbed_temp")
    )
    target_nozzle_temp = _to_float(
        raw.get("target_nozzle_temp")
        or param.get("target_nozzle_temp")
        or proj_settings.get("target_nozzle_temp")
    )
    target_bed_temp = _to_float(
        raw.get("target_hotbed_temp")
        or param.get("target_hotbed_temp")
        or proj_settings.get("target_hotbed_temp")
    )

    has_live_temps = nozzle_temp is not None or bed_temp is not None
    if (
        status == "printing"
        and has_live_temps
        and current_layer == 0
        and total_layers == 0
        and remaining_time_min == 0
        and (target_nozzle_temp or 0.0) <= 0.0
        and (target_bed_temp or 0.0) <= 0.0
    ):
        status = "downloading"

    if status == "printing" and current_layer == 0 and total_layers > 0 and progress > 0:
        current_layer = max(1, min(total_layers, round(total_layers * progress / 100.0)))

    base_info = raw.get("base") if isinstance(raw.get("base"), dict) else {}
    raw_fw = (
        raw.get("firmware_version")
        or base_info.get("firmware_version")
        or raw.get("version")
    )
    if isinstance(raw_fw, dict):
        raw_fw = raw_fw.get("firmware_version") or raw_fw.get("version") or ""
    firmware_version = str(raw_fw or "").strip()

    return PrinterSummary(
        printer_id=pid,
        name=name,
        model=model,
        online=online,
        status=status,
        progress=max(0, min(100, progress)),
        current_file=current_file,
        raw_state=raw_state,
        nozzle_temp=nozzle_temp,
        bed_temp=bed_temp,
        target_nozzle_temp=target_nozzle_temp,
        target_bed_temp=target_bed_temp,
        remaining_time_min=max(0, remaining_time_min),
        current_layer=max(0, current_layer),
        total_layers=max(0, total_layers),
        project_id=project_id,
        firmware_version=firmware_version,
        printer_key=str(raw.get("key") or raw.get("printer_key") or "").strip(),
        machine_type=str(raw.get("machine_type") or "").strip(),
        total_print_time=str(base_info.get("print_totaltime") or "").strip(),
        material_used=str(base_info.get("material_used") or "").strip(),
        print_count=_to_int(base_info.get("print_count"), 0),
    )


class _ChunkedFileReader:
    """File-like wrapper that streams bytes to urllib PUT and reports progress."""

    def __init__(
        self,
        file_path: Path,
        total_size: int,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_cb: Optional[CancelCallback] = None,
        chunk_size: int = 256 * 1024,
    ) -> None:
        self._fp = file_path.open("rb")
        self._total = max(1, total_size)
        self._sent = 0
        self._progress_cb = progress_cb
        self._cancel_cb = cancel_cb
        self._chunk_size = chunk_size
        self._last_pct = -1

    def __len__(self) -> int:
        return self._total

    def read(self, size: int = -1) -> bytes:
        if self._cancel_cb and self._cancel_cb():
            raise AnycubicUploadError("Загрузка отменена пользователем.")
        to_read = self._chunk_size if size is None or size < 0 else min(size, self._chunk_size)
        chunk = self._fp.read(to_read)
        if chunk:
            self._sent += len(chunk)
            if self._progress_cb:
                # Map upload bytes into 15%..85% of total workflow progress
                pct = 15 + int((self._sent / self._total) * 70)
                if pct != self._last_pct:
                    self._last_pct = pct
                    self._progress_cb(pct, f"Загрузка в Anycubic Cloud ({int(self._sent * 100 / self._total)}%)...")
        return chunk

    def close(self) -> None:
        self._fp.close()


def build_ssl_context() -> ssl.SSLContext:
    """Create a verified SSLContext with fallback CA bundles for embedded Python (Windows/macOS/Linux)."""
    ctx = ssl.create_default_context()
    ca_candidates: List[str] = []
    try:
        import certifi  # type: ignore[import-not-found]

        ca_candidates.append(certifi.where())
    except Exception:
        pass
    try:
        from pip._vendor import certifi as pip_certifi  # type: ignore[import-not-found]

        ca_candidates.append(pip_certifi.where())
    except Exception:
        pass
    ca_candidates.extend(
        [
            "/etc/ssl/cert.pem",
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/pki/tls/certs/ca-bundle.crt",
        ]
    )
    for ca_file in ca_candidates:
        if ca_file and Path(ca_file).is_file():
            try:
                ctx.load_verify_locations(cafile=ca_file)
                break
            except Exception:
                continue
    return ctx


class AnycubicCloudClient:
    """Synchronous Anycubic Cloud REST client (stdlib only, zero dependencies)."""

    def __init__(
        self,
        token: str,
        api_root: str = PUBLIC_API_ROOT,
        timeout: int = 30,
        ssl_context: Optional[ssl.SSLContext] = None,
    ) -> None:
        self._auth = AnycubicAuth(token=token)
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout
        self._ssl_context = ssl_context or build_ssl_context()

    def _request_json(
        self,
        method: str,
        endpoint: str,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        with_token: bool = True,
    ) -> Dict[str, Any]:
        url = f"{self._api_root}{endpoint}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = self._auth.build_headers(with_token=with_token)
        data_bytes: Optional[bytes] = None
        if method.upper() == "POST":
            data_bytes = json.dumps(body if body is not None else {}).encode("utf-8")

        req = urllib.request.Request(
            url=url,
            data=data_bytes,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(
                req, timeout=self._timeout, context=self._ssl_context
            ) as resp:
                raw_text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AnycubicTokenExpiredError(
                    "Срок действия токена Anycubic Cloud истёк (HTTP "
                    f"{exc.code}). Войдите в аккаунт повторно."
                ) from exc
            raise AnycubicError(
                f"Ошибка HTTP {exc.code} при обращении к Anycubic Cloud ({endpoint})."
            ) from exc
        except urllib.error.URLError as exc:
            raise AnycubicError(
                f"Connection to Anycubic Cloud failed: {exc.reason}"
            ) from exc

        try:
            parsed = json.loads(raw_text)
        except ValueError as exc:
            raise AnycubicError(
                f"Некорректный JSON-ответ от Anycubic Cloud ({endpoint})."
            ) from exc

        if not isinstance(parsed, dict):
            raise AnycubicError("Неожиданный формат ответа Anycubic Cloud.")

        code = parsed.get("code")
        msg = str(parsed.get("msg") or parsed.get("message") or "")
        msg_lower = msg.lower()

        if code in AUTH_ERROR_CODES or any(
            kw in msg_lower for kw in ("token", "unauthorized", "login", "expired", "auth")
        ) and code not in (1, 0, None):
            raise AnycubicTokenExpiredError(
                "Токен Anycubic Cloud недействителен или истёк "
                f"(code={code}, msg={msg!r}). Войдите заново."
            )

        if code is not None and code != 1:
            raise AnycubicError(
                f"Anycubic Cloud вернул ошибку (code={code}): {msg or 'без описания'}"
            )

        return parsed

    def get_user_info(self) -> Dict[str, Any]:
        """Validate token and fetch basic user profile."""
        resp = self._request_json("GET", EP_USER_INFO)
        data = resp.get("data")
        if not isinstance(data, dict):
            raise AnycubicTokenExpiredError(
                "Токен Anycubic Cloud истёк или отклонён сервером. Войдите повторно."
            )
        return {
            "id": data.get("id"),
            "email": data.get("user_email") or data.get("email") or "",
            "nickname": data.get("user_nickname") or data.get("nickname") or "",
        }

    def get_printers(self) -> List[PrinterSummary]:
        """Return list of printers bound to the Anycubic Cloud account."""
        resp = self._request_json("GET", EP_GET_PRINTERS)
        data = resp.get("data")
        if not isinstance(data, list):
            return []
        return [_normalize_printer(item) for item in data if isinstance(item, dict)]

    def get_printer_status(self, printer_id: int) -> PrinterSummary:
        """Fetch live status for a specific printer."""
        resp = self._request_json("GET", EP_PRINTER_INFO, query={"id": str(printer_id)})
        data = resp.get("data")
        if not isinstance(data, dict):
            raise AnycubicError(f"Принтер #{printer_id} не найден в Anycubic Cloud.")
        return _normalize_printer(data)

    def get_printers_with_live_status(self) -> List[PrinterSummary]:
        """Fetch printers list and enrich each with live /v2/printer/info status."""
        printers = self.get_printers()
        enriched: List[PrinterSummary] = []
        for p in printers:
            try:
                live = self.get_printer_status(p.printer_id)
                if not live.model and p.model:
                    live.model = p.model
                if not live.name and p.name:
                    live.name = p.name
                enriched.append(live)
            except AnycubicError:
                enriched.append(p)
        return enriched

    def _unlock_storage_space(self, lock_id: int, delete_cos: bool = True) -> None:
        try:
            self._request_json(
                "POST",
                EP_UNLOCK_STORAGE,
                body={"id": int(lock_id), "is_delete_cos": int(bool(delete_cos))},
            )
        except AnycubicError:
            pass

    def _put_presigned(
        self,
        presigned_url: str,
        file_path: Path,
        file_size: int,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_cb: Optional[CancelCallback] = None,
    ) -> None:
        reader = _ChunkedFileReader(
            file_path=file_path,
            total_size=file_size,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
        req = urllib.request.Request(
            url=presigned_url,
            data=reader,  # type: ignore[arg-type]
            headers={"Content-Length": str(file_size)},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(
                req, timeout=max(120, self._timeout), context=self._ssl_context
            ) as resp:
                if resp.status not in (200, 201, 204):
                    raise AnycubicUploadError(
                        f"Хранилище Anycubic вернуло HTTP {resp.status} при загрузке файла."
                    )
        except urllib.error.HTTPError as exc:
            raise AnycubicUploadError(
                f"Ошибка загрузки файла в облачное хранилище (HTTP {exc.code})."
            ) from exc
        except urllib.error.URLError as exc:
            raise AnycubicUploadError(
                f"Сбой сети при загрузке файла в облачное хранилище: {exc.reason}"
            ) from exc
        finally:
            reader.close()

    def upload_gcode(
        self,
        file_path: str | Path,
        remote_name: Optional[str] = None,
        is_temp_file: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_cb: Optional[CancelCallback] = None,
    ) -> int:
        """Upload a .gcode file to Anycubic Cloud and return its cloud file_id."""
        path = Path(file_path)
        if not path.is_file():
            raise AnycubicUploadError(f"Файл не найден: {path}")

        file_size = path.stat().st_size
        if file_size <= 0:
            raise AnycubicUploadError("Нельзя загрузить пустой G-code файл.")

        name = (remote_name or path.name).strip()
        if not name.lower().endswith(".gcode"):
            name = f"{name}.gcode"

        if progress_cb:
            progress_cb(5, "Резервирование места в Anycubic Cloud...")

        lock_resp = self._request_json(
            "POST",
            EP_LOCK_STORAGE,
            body={"size": file_size, "name": name, "is_temp_file": int(bool(is_temp_file))},
        )
        lock_data = lock_resp.get("data") or {}
        lock_id = lock_data.get("id")
        presigned_url = lock_data.get("preSignUrl")
        if not lock_id or not presigned_url:
            raise AnycubicUploadError(
                "Anycubic Cloud не вернул ссылку для загрузки файла (lockStorageSpace)."
            )

        try:
            if progress_cb:
                progress_cb(15, "Загрузка G-code в Anycubic Cloud...")
            self._put_presigned(
                presigned_url=str(presigned_url),
                file_path=path,
                file_size=file_size,
                progress_cb=progress_cb,
                cancel_cb=cancel_cb,
            )

            if progress_cb:
                progress_cb(90, "Регистрация файла в облачной библиотеке...")

            claim_resp = self._request_json(
                "POST",
                EP_NEW_UPLOAD_FILE,
                body={"user_lock_space_id": int(lock_id)},
            )
            claim_data = claim_resp.get("data") or {}
            file_id = claim_data.get("id")
            if not file_id:
                raise AnycubicUploadError(
                    "Не удалось подтвердить загрузку файла (newUploadFile)."
                )
            return int(file_id)
        except Exception:
            self._unlock_storage_space(int(lock_id), delete_cos=True)
            raise

    def start_print(
        self,
        printer_id: int,
        file_id: int,
        is_delete_file: bool = False,
        camera_timelapse: bool = False,
        ams_box_mapping: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Start printing an already-uploaded cloud file on any Anycubic Cloud FDM printer."""
        order_data: Dict[str, Any] = {
            "filetype": 0,
            "file_key": "",
            "file_name": "",
            "file_id": int(file_id),
            "hollow_param": None,
            "is_delete_file": int(bool(is_delete_file)),
            "matrix": "",
            "project_type": 1,
            "punching_param": None,
            "slice_param": None,
            "slice_size": None,
            "template_id": 0,
            "task_settings": {
                "ai_detect": 0,
                "camera_timelapse": int(bool(camera_timelapse)),
            },
        }
        if ams_box_mapping:
            order_data["ams_box_mapping"] = ams_box_mapping
        payload = {
            "order_id": ORDER_ID_START_PRINT,
            "printer_id": int(printer_id),
            "project_id": 0,
            "data": order_data,
            "ams_info": None,
            "settings": None,
        }
        return self._request_json("POST", EP_SEND_ORDER, body=payload)

    def get_cloud_storage(self) -> Dict[str, Any]:
        """Return cloud storage usage info (used_bytes, total_bytes, label)."""
        resp = self._request_json("POST", EP_USER_STORE, body={})
        data = resp.get("data")
        if not isinstance(data, dict):
            return {"used_mb": 0.0, "total_mb": 0.0, "label": ""}
        used = _to_int(data.get("used"), 0)
        total = _to_int(data.get("total"), 0)
        used_str = str(data.get("used_str") or "")
        total_str = str(data.get("total_str") or "")
        if used_str and total_str:
            label = f"{used_str} / {total_str}"
        elif total > 0:
            label = f"{used / (1024 * 1024):.1f} MB / {total / (1024 * 1024 * 1024):.2f} GB"
        else:
            label = ""
        return {
            "used_bytes": used,
            "total_bytes": total,
            "label": label,
        }

    def get_cloud_files(self, page: int = 1, limit: int = 20) -> List[CloudFileSummary]:
        """Return list of printable files stored in Anycubic Cloud."""
        resp = self._request_json(
            "POST",
            EP_USER_FILES,
            body={"page": int(page), "limit": int(limit), "printable": 1, "machine_type": 0},
        )
        data = resp.get("data")
        if not isinstance(data, list):
            return []
        files: List[CloudFileSummary] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            fid = _to_int(item.get("id"), 0)
            if fid <= 0:
                continue
            gid = _to_int(item.get("gcode_id"), 0)
            name = str(item.get("old_filename") or item.get("filename") or f"file_{fid}.gcode")
            size_bytes = _to_int(item.get("size"), 0)
            created_at = str(item.get("time") or item.get("create_time") or "")
            url = str(item.get("url") or "")
            files.append(
                CloudFileSummary(
                    file_id=fid,
                    gcode_id=gid,
                    name=name,
                    size_bytes=size_bytes,
                    created_at=created_at,
                    url=url,
                )
            )
        return files

    def delete_cloud_file(self, file_id: int) -> bool:
        """Delete a file from Anycubic Cloud storage."""
        self._request_json("POST", EP_DELETE_CLOUD_FILE, body={"idArr": [int(file_id)]})
        return True

    def _upload_bytes_temp(self, data: bytes, remote_name: str) -> int:
        """Upload raw .gcode bytes as a temporary print job (is_temp_file=1)."""
        if not data:
            raise AnycubicUploadError("Пустой файл G-code.")
        name = remote_name.strip() or "print.gcode"
        if not name.lower().endswith(".gcode"):
            name = f"{name}.gcode"

        lock_resp = self._request_json(
            "POST",
            EP_LOCK_STORAGE,
            body={"size": len(data), "name": name, "is_temp_file": 1},
        )
        lock_data = lock_resp.get("data") or {}
        lock_id = lock_data.get("id")
        presigned_url = lock_data.get("preSignUrl")
        if not lock_id or not presigned_url:
            raise AnycubicUploadError("Anycubic Cloud не вернул ссылку для загрузки файла.")

        try:
            req = urllib.request.Request(
                url=str(presigned_url),
                data=data,
                headers={"Content-Length": str(len(data))},
                method="PUT",
            )
            with urllib.request.urlopen(
                req, timeout=max(120, self._timeout), context=self._ssl_context
            ) as resp:
                if resp.status not in (200, 201, 204):
                    raise AnycubicUploadError(f"Ошибка загрузки в хранилище (HTTP {resp.status}).")
            claim_resp = self._request_json(
                "POST",
                EP_NEW_UPLOAD_FILE,
                body={"user_lock_space_id": int(lock_id)},
            )
            claim_data = claim_resp.get("data") or {}
            fid = claim_data.get("id")
            if not fid:
                raise AnycubicUploadError("Не удалось подтвердить загрузку файла (newUploadFile).")
            return int(fid)
        except Exception:
            self._unlock_storage_space(int(lock_id), delete_cos=True)
            raise

    def start_print_cloud_file(
        self,
        printer_id: int,
        file_id: int,
        gcode_id: int = 0,
        file_url: str = "",
        file_name: str = "print.gcode",
    ) -> Dict[str, Any]:
        """Start printing a saved cloud file (tries direct cloud print, falls back to temp print job)."""
        if gcode_id > 0:
            try:
                return self.start_print(
                    printer_id=int(printer_id),
                    file_id=int(file_id),
                    is_delete_file=False,
                )
            except AnycubicError as exc:
                err_l = str(exc).lower()
                if "no slicer found" not in err_l and "no file found" not in err_l:
                    raise

        if not file_url:
            for f in self.get_cloud_files(limit=50):
                if f.file_id == int(file_id) and f.url:
                    file_url = f.url
                    file_name = f.name or file_name
                    break
        if not file_url:
            raise AnycubicError(
                "Не удалось получить ссылку на облачный файл для запуска печати."
            )

        try:
            req = urllib.request.Request(url=file_url, method="GET")
            with urllib.request.urlopen(
                req, timeout=max(120, self._timeout), context=self._ssl_context
            ) as resp:
                raw_bytes = resp.read()
        except Exception as exc:
            raise AnycubicError(
                f"Не удалось скачать файл из облака для запуска печати: {exc}"
            ) from exc

        temp_fid = self._upload_bytes_temp(raw_bytes, remote_name=file_name)
        last_err: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self.start_print(
                    printer_id=int(printer_id),
                    file_id=temp_fid,
                    is_delete_file=True,
                )
            except AnycubicError as exc:
                last_err = exc
                err_l = str(exc).lower()
                if (
                    "no file found" in err_l or "no slicer found" in err_l
                ) and attempt < 2:
                    time.sleep(2.5)
                    continue
                raise
        assert last_err is not None
        raise last_err

    def get_active_project_id(self, printer_id: int) -> int:
        """Resolve the active Anycubic Cloud project_id for pause/resume/stop orders."""
        try:
            st = self.get_printer_status(printer_id)
            if st.project_id > 0:
                return st.project_id
        except AnycubicError:
            pass

        try:
            resp = self._request_json(
                "GET",
                EP_GET_PROJECTS,
                query={"printer_id": str(printer_id), "print_status": "1", "limit": "1"},
            )
            data = resp.get("data")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    item_printer_id = _to_int(item.get("printer_id"), 0)
                    if item_printer_id and item_printer_id != int(printer_id):
                        continue
                    pid = _to_int(item.get("id") or item.get("project_id"), 0)
                    if pid > 0:
                        return pid
        except AnycubicError:
            pass

        return 0

    def pause_print(self, printer_id: int, project_id: int = 0) -> Dict[str, Any]:
        """Pause the active print job on the printer."""
        pid = int(project_id) or self.get_active_project_id(printer_id)
        return self._request_json(
            "POST",
            EP_SEND_ORDER,
            body={
                "order_id": ORDER_ID_PAUSE_PRINT,
                "printer_id": int(printer_id),
                "project_id": pid,
                "data": None,
                "ams_info": None,
                "settings": None,
            },
        )

    def resume_print(self, printer_id: int, project_id: int = 0) -> Dict[str, Any]:
        """Resume a paused print job on the printer."""
        pid = int(project_id) or self.get_active_project_id(printer_id)
        return self._request_json(
            "POST",
            EP_SEND_ORDER,
            body={
                "order_id": ORDER_ID_RESUME_PRINT,
                "printer_id": int(printer_id),
                "project_id": pid,
                "data": None,
                "ams_info": None,
                "settings": None,
            },
        )

    def stop_print(self, printer_id: int, project_id: int = 0) -> Dict[str, Any]:
        """Cancel/stop the active print job on the printer."""
        pid = int(project_id) or self.get_active_project_id(printer_id)
        return self._request_json(
            "POST",
            EP_SEND_ORDER,
            body={
                "order_id": ORDER_ID_STOP_PRINT,
                "printer_id": int(printer_id),
                "project_id": pid,
                "data": None,
                "ams_info": None,
                "settings": None,
            },
        )

    def update_print_settings(
        self,
        printer_id: int,
        project_id: int = 0,
        settings: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update live print settings (speed mode, temps, fan) during printing."""
        pid = int(project_id) or self.get_active_project_id(printer_id)
        return self._request_json(
            "POST",
            EP_SEND_ORDER,
            body={
                "order_id": ORDER_ID_PRINT_SETTINGS,
                "printer_id": int(printer_id),
                "project_id": pid,
                "data": None,
                "ams_info": None,
                "settings": settings or {},
            },
        )

    def upload_and_print(
        self,
        printer_id: int,
        file_path: str | Path,
        remote_name: Optional[str] = None,
        start_print: bool = True,
        camera_timelapse: bool = False,
        ams_box_mapping: Optional[List[Dict[str, Any]]] = None,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_cb: Optional[CancelCallback] = None,
    ) -> Dict[str, Any]:
        """Upload G-code and optionally start printing immediately."""
        file_id = self.upload_gcode(
            file_path=file_path,
            remote_name=remote_name,
            is_temp_file=start_print,
            progress_cb=progress_cb,
            cancel_cb=cancel_cb,
        )
        result: Dict[str, Any] = {
            "file_id": file_id,
            "printer_id": int(printer_id),
            "started": False,
        }
        if start_print:
            if progress_cb:
                progress_cb(95, "Запуск печати на принтере...")
            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    self.start_print(
                        printer_id=printer_id,
                        file_id=file_id,
                        is_delete_file=True,
                        camera_timelapse=camera_timelapse,
                        ams_box_mapping=ams_box_mapping,
                    )
                    result["started"] = True
                    last_err = None
                    break
                except AnycubicError as exc:
                    last_err = exc
                    err_lower = str(exc).lower()
                    if (
                        "no file found" in err_lower or "no slicer found" in err_lower
                    ) and attempt < 2:
                        time.sleep(2.5)
                        continue
                    raise
            if last_err is not None:
                raise last_err
        if progress_cb:
            progress_cb(100, "Готово!")
        return result
