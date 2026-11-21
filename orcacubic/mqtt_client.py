"""Pure-stdlib MQTT 3.1.1 over TLS client for Anycubic Cloud live telemetry.

Zero external dependencies (no paho-mqtt, no cryptography):
- Uses stdlib `ssl` + `socket` for `mqtts://mqtt-universe.anycubic.com:8883`.
- Implements RSA PKCS#1 v1.5 encryption via Python's built-in `pow(m, e, n)`.
- Receives real-time nozzle/bed current & target temperatures, print progress,
  layer counts, remaining time, and printer state transitions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

MQTT_HOST = "mqtt-universe.anycubic.com"
MQTT_PORT = 8883
MQTT_KEEPALIVE = 45
MQTT_SUB_TEMPLATE = "anycubic/anycubicCloud/v1/+/public/{machine_type}/{printer_key}/#"

# Public RSA-2048 key of Anycubic Cloud MQTT broker (used to encrypt workbench JWT password).
_BROKER_PUB_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAn2Kpx1AzT2Df1kf1MImT"
    "dqCgq5JDGdFTMw/Mx4TN6+oYUQyVUcSs8j+EPFlaCja7t9nXoNLrfS26PwdDzJEt"
    "+wVDCdoWvNxuie+0w2Zk8lpRHn76z+waTONO3OLaneWe0xINNMtAqmuV0w2HSoNm"
    "vrLqJ7iO8moNeUO92fHUAnwkrNCtKeWpB80DLdVJBZRUeiL7KKH+7ud343KcfWLp"
    "C5cj9RyZYVCEShwY9/tPN40Jg+jk43JiFbu5ruAMPuUWn5Iy/6ENnyBKk21uYxED"
    "3y/OKNVJyl8+fZMgZriCzIP2YsU52O7nbr2iWytA1a1ZTIyiUPpGYQSYY8PSV7vw"
    "xwIDAQAB"
)


def _parse_rsa_pub_spki(b64_der: str) -> Tuple[int, int, int]:
    """Extract (n, e, key_bytes) from an RSA SubjectPublicKeyInfo DER blob."""
    der = base64.b64decode(b64_der)
    # Locate BIT STRING (0x03) wrapping RSAPublicKey SEQUENCE (0x30)
    idx = der.index(b"\x03\x82\x01\x0f\x00") + 5
    if der[idx] != 0x30:
        raise ValueError("Invalid RSAPublicKey SEQUENCE")
    idx += 4 if der[idx + 1] == 0x82 else 3
    if der[idx] != 0x02:
        raise ValueError("Invalid RSA modulus tag")
    n_len = int.from_bytes(der[idx + 2 : idx + 4], "big")
    n = int.from_bytes(der[idx + 4 : idx + 4 + n_len], "big")
    idx += 4 + n_len
    if der[idx] != 0x02:
        raise ValueError("Invalid RSA exponent tag")
    e_len = der[idx + 1]
    e = int.from_bytes(der[idx + 2 : idx + 2 + e_len], "big")
    return n, e, (n.bit_length() + 7) // 8


_RSA_N, _RSA_E, _RSA_K = _parse_rsa_pub_spki(_BROKER_PUB_B64)


def rsa_pkcs1_v15_encrypt(message: bytes) -> bytes:
    """Encrypt message with Anycubic MQTT broker public key (RSAES-PKCS1-v1_5)."""
    ps_len = _RSA_K - len(message) - 3
    if ps_len < 8:
        raise ValueError("Message too long for RSA-2048 PKCS#1 v1.5")
    ps = bytearray()
    while len(ps) < ps_len:
        chunk = os.urandom(ps_len - len(ps) + 8)
        for b in chunk:
            if b != 0:
                ps.append(b)
                if len(ps) == ps_len:
                    break
    em = b"\x00\x02" + bytes(ps) + b"\x00" + message
    ct = pow(int.from_bytes(em, "big"), _RSA_E, _RSA_N)
    return ct.to_bytes(_RSA_K, "big")


def build_mqtt_credentials(
    token: str, email: str, prefix: str = "web"
) -> Tuple[str, str, str]:
    """Return (client_id, username, password) for mqtt-universe.anycubic.com."""
    tok_bytes = token.encode("utf-8")
    m = hashlib.md5(tok_bytes).hexdigest()
    enc = base64.b64encode(rsa_pkcs1_v15_encrypt(tok_bytes)).decode("ascii")
    client_id = f"{prefix}_{m}"
    md5b = hashlib.md5(f"{client_id}{enc}{client_id}".encode("utf-8")).hexdigest()
    username = f"user|{prefix}|{email}|{md5b}"
    return client_id, username, enc


def _encode_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n > 0:
            digit |= 0x80
        out.append(digit)
        if n == 0:
            break
    return bytes(out)


def _build_connect_packet(
    client_id: str, username: str, password: str, keepalive: int = MQTT_KEEPALIVE
) -> bytes:
    # Protocol Name "MQTT", Level 4 (3.1.1), Flags 0xC2 (user+pass+clean_session)
    var_hdr = _encode_str("MQTT") + struct.pack("!BBH", 4, 0xC2, keepalive)
    payload = _encode_str(client_id) + _encode_str(username) + _encode_str(password)
    body = var_hdr + payload
    return bytes([0x10]) + _encode_remaining_length(len(body)) + body


def _build_subscribe_packet(packet_id: int, topics: List[str]) -> bytes:
    var_hdr = struct.pack("!H", packet_id & 0xFFFF)
    payload = b"".join(_encode_str(t) + b"\x00" for t in topics)
    body = var_hdr + payload
    return bytes([0x82]) + _encode_remaining_length(len(body)) + body


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("MQTT socket closed")
        buf.extend(chunk)
    return bytes(buf)


def _read_packet(sock: socket.socket) -> Tuple[int, bytes]:
    first = _recv_exact(sock, 1)[0]
    mult = 1
    rem_len = 0
    while True:
        b = _recv_exact(sock, 1)[0]
        rem_len += (b & 0x7F) * mult
        if (b & 0x80) == 0:
            break
        mult *= 128
        if mult > 128 * 128 * 128:
            raise ValueError("Malformed MQTT remaining length")
    payload = _recv_exact(sock, rem_len) if rem_len > 0 else b""
    return first, payload


def parse_mqtt_telemetry(payload_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Extract normalized telemetry patch fields from an Anycubic Cloud MQTT JSON event."""
    patch: Dict[str, Any] = {}
    if not isinstance(payload_dict, dict):
        return patch

    data = payload_dict.get("data")
    if isinstance(data, str) and data.startswith("{"):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}

    temp_obj = data.get("temp") if isinstance(data.get("temp"), dict) else data
    for src, dst in (
        ("curr_nozzle_temp", "nozzle_temp"),
        ("target_nozzle_temp", "target_nozzle_temp"),
        ("curr_hotbed_temp", "bed_temp"),
        ("target_hotbed_temp", "target_bed_temp"),
    ):
        val = temp_obj.get(src)
        if val is not None and val != "":
            try:
                patch[dst] = float(val)
            except (ValueError, TypeError):
                pass

    state_str = str(payload_dict.get("state") or data.get("state") or "").lower().strip()
    if state_str in ("free", "idle", "finished", "stop", "stopped"):
        patch["status"] = "idle"
    elif state_str in ("downloading", "download", "receiving"):
        patch["status"] = "downloading"
    elif state_str in ("printing", "running", "heating", "leveling"):
        patch["status"] = "printing"
    elif state_str in ("pause", "paused"):
        patch["status"] = "paused"

    if data.get("progress") is not None:
        try:
            patch["progress"] = max(0, min(100, int(float(data["progress"]))))
        except (ValueError, TypeError):
            pass
    for layer_key in ("curr_layer", "current_layer"):
        if data.get(layer_key) is not None:
            try:
                lv = int(float(data[layer_key]))
                if lv >= 0:
                    patch["current_layer"] = lv
            except (ValueError, TypeError):
                pass
    for total_key in ("total_layers", "total_layer"):
        if data.get(total_key) is not None:
            try:
                tv = int(float(data[total_key]))
                if tv >= 0:
                    patch["total_layers"] = tv
            except (ValueError, TypeError):
                pass
    if data.get("remain_time") is not None:
        try:
            rv = int(float(data["remain_time"]))
            if rv >= 0:
                patch["remaining_time_min"] = rv
        except (ValueError, TypeError):
            pass
    fname = data.get("filename") or data.get("gcode_name")
    if isinstance(fname, str) and fname.strip():
        patch["current_file"] = fname.strip()

    return patch


class AnycubicMqttWatcher:
    """Background daemon that streams live Anycubic Cloud MQTT telemetry for printers."""

    def __init__(
        self,
        on_telemetry: Callable[[int, Dict[str, Any]], None],
    ) -> None:
        self._on_telemetry = on_telemetry
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._token = ""
        self._email = ""
        # List of (printer_id, machine_type, printer_key)
        self._targets: List[Tuple[int, str, str]] = []
        self._sock: Optional[socket.socket] = None

    def configure(
        self,
        token: str,
        email: str,
        targets: List[Tuple[int, str, str]],
    ) -> None:
        with self._lock:
            changed = (
                token != self._token
                or email != self._email
                or sorted(targets) != sorted(self._targets)
            )
            self._token = token
            self._email = email
            self._targets = list(targets)
            sock = self._sock if changed else None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if token and targets and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = 2.0
        pkt_id = 1
        while not self._stop.is_set():
            with self._lock:
                token = self._token
                email = self._email
                targets = list(self._targets)
            if not token or not targets:
                self._stop.wait(2.0)
                continue

            key_to_pid = {key: pid for pid, _mt, key in targets if key}
            topics = [
                MQTT_SUB_TEMPLATE.format(machine_type=mt, printer_key=key)
                for _pid, mt, key in targets
                if mt and key
            ]
            if not topics:
                self._stop.wait(5.0)
                continue

            sock: Optional[socket.socket] = None
            try:
                cid, uname, pwd = build_mqtt_credentials(token, email, prefix="web")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                raw = socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=10.0)
                sock = ctx.wrap_socket(raw, server_hostname=MQTT_HOST)
                sock.settimeout(10.0)
                with self._lock:
                    self._sock = sock

                sock.sendall(_build_connect_packet(cid, uname, pwd, MQTT_KEEPALIVE))
                hdr, body = _read_packet(sock)
                if (hdr >> 4) != 2 or len(body) < 2 or body[1] != 0:
                    raise ConnectionError(f"MQTT CONNACK rejected: rc={body[1] if len(body)>=2 else '?'}")

                pkt_id = (pkt_id % 65000) + 1
                sock.sendall(_build_subscribe_packet(pkt_id, topics))
                backoff = 2.0
                last_ping = time.monotonic()
                sock.settimeout(5.0)

                while not self._stop.is_set():
                    if time.monotonic() - last_ping >= MQTT_KEEPALIVE - 5:
                        sock.sendall(b"\xC0\x00")  # PINGREQ
                        last_ping = time.monotonic()
                    try:
                        hdr, body = _read_packet(sock)
                    except socket.timeout:
                        continue

                    ptype = hdr >> 4
                    if ptype == 3:  # PUBLISH
                        if len(body) < 2:
                            continue
                        tlen = struct.unpack("!H", body[:2])[0]
                        topic = body[2 : 2 + tlen].decode("utf-8", errors="ignore")
                        qos = (hdr >> 1) & 0x03
                        pos = 2 + tlen + (2 if qos > 0 else 0)
                        msg_bytes = body[pos:]
                        matched_pid = None
                        for pkey, pid in key_to_pid.items():
                            if f"/{pkey}/" in topic:
                                matched_pid = pid
                                break
                        if matched_pid is not None and msg_bytes:
                            try:
                                parsed = json.loads(msg_bytes.decode("utf-8", errors="ignore"))
                                patch = parse_mqtt_telemetry(parsed)
                                if patch:
                                    self._on_telemetry(matched_pid, patch)
                            except Exception:
                                pass
            except Exception:
                pass
            finally:
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 2.0)
