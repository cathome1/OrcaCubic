"""Unit tests for OrcaCubic client, auth, config, and OrcaSlicer plugin."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from orcacubic.auth import (
    AC_APP_ID,
    AC_APP_SECRET,
    AC_APP_VERSION_WEB,
    AnycubicAuth,
    AnycubicAuthError,
    AnycubicTokenExpiredError,
    AnycubicUploadError,
)
from orcacubic.camera import build_kvs_presigned_wss_url
from orcacubic.client import AnycubicCloudClient
from orcacubic.gcode_adapter import adapt_gcode_text
from orcacubic.config import (
    OrcaCubicConfig,
    load_config,
    mask_token,
    save_config,
    try_read_slicer_next_token,
)


class _FakeHTTPResponse:
    def __init__(self, payload: dict | str, status: int = 200) -> None:
        self._body = (
            json.dumps(payload).encode("utf-8")
            if isinstance(payload, dict)
            else payload.encode("utf-8")
        )
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class TestAuthAndConfig(unittest.TestCase):
    def test_signature_matches_expected_md5(self) -> None:
        auth = AnycubicAuth(token="test-token-1234567890")
        headers = auth.build_headers(timestamp_ms=1700000000000, nonce="fixed-nonce")
        expected_input = (
            f"{AC_APP_ID}1700000000000{AC_APP_VERSION_WEB}"
            f"{AC_APP_SECRET}fixed-nonce{AC_APP_ID}"
        )
        expected_sig = hashlib.md5(expected_input.encode("utf-8")).hexdigest()
        self.assertEqual(headers["Xx-Signature"], expected_sig)
        self.assertEqual(headers["XX-Token"], "test-token-1234567890")
        self.assertEqual(headers["Xx-Nonce"], "fixed-nonce")

    def test_missing_token_raises(self) -> None:
        auth = AnycubicAuth(token="")
        with self.assertRaises(AnycubicAuthError):
            auth.build_headers(with_token=True)

    def test_save_and_load_config_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg = OrcaCubicConfig(
                token="secret_token_abcdef1234",
                printer_id=42,
                printer_name="Kobra 2 Pro",
                default_mode="upload_only",
            )
            save_config(cfg, path=cfg_path)
            mode = stat.S_IMODE(cfg_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANYCUBIC_TOKEN", None)
                loaded = load_config(path=cfg_path)
            self.assertEqual(loaded.token, "secret_token_abcdef1234")
            self.assertEqual(loaded.printer_id, 42)
            self.assertEqual(loaded.default_mode, "upload_only")
            self.assertEqual(mask_token(loaded.token), "secr…1234")

    def test_encrypted_slicer_next_conf_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "AnycubicSlicerNext.conf"
            conf.write_text(
                json.dumps({"anycubic_cloud": {"l1qde7GcMLvhaKcXOcWRLA==": "encrypted"}}),
                encoding="utf-8",
            )
            token, msg = try_read_slicer_next_token(conf)
            self.assertIsNone(token)
            self.assertIn("шифрует", msg)


class TestAnycubicClient(unittest.TestCase):
    def test_get_printers_normalization(self) -> None:
        client = AnycubicCloudClient(token="tok_12345678901234567890")
        fake_resp = _FakeHTTPResponse(
            {
                "code": 1,
                "data": [
                    {
                        "id": 101,
                        "name": "My Kobra 2 Pro",
                        "machine_name": "Anycubic Kobra 2 Pro",
                        "device_status": 1,
                        "is_printing": 1,
                    },
                    {
                        "id": 102,
                        "name": "Busy Printer",
                        "machine_name": "Kobra 2 Pro",
                        "device_status": 1,
                        "is_printing": 2,
                        "project": {"progress": 67, "gcode_name": "benchy.gcode"},
                    },
                ],
            }
        )
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            printers = client.get_printers()

        self.assertEqual(len(printers), 2)
        self.assertEqual(printers[0].status, "idle")
        self.assertTrue(printers[0].online)
        self.assertEqual(printers[1].status, "printing")
        self.assertEqual(printers[1].progress, 67)
        self.assertEqual(printers[1].current_file, "benchy.gcode")

    def test_expired_token_error(self) -> None:
        client = AnycubicCloudClient(token="expired_token_1234567890")
        fake_resp = _FakeHTTPResponse({"code": 401, "msg": "token expired"})
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertRaises(AnycubicTokenExpiredError):
                client.get_user_info()

    def test_upload_and_start_print_sequence(self) -> None:
        client = AnycubicCloudClient(token="valid_token_1234567890")
        calls: list[tuple[str, str, dict | None]] = []

        def fake_urlopen(req, timeout=30, context=None):
            url = req.full_url
            method = req.get_method()
            body = None
            if method == "POST" and req.data:
                body = json.loads(req.data.decode("utf-8"))
            elif method == "PUT" and req.data:
                # Drain chunked reader to verify progress callbacks
                while req.data.read():
                    pass
            calls.append((method, url, body))

            if url.endswith("/v2/cloud_storage/lockStorageSpace"):
                return _FakeHTTPResponse(
                    {
                        "code": 1,
                        "data": {
                            "id": 999,
                            "preSignUrl": "https://s3.amazonaws.com/bucket/test.gcode?sig=1",
                        },
                    }
                )
            if url.startswith("https://s3.amazonaws.com/"):
                return _FakeHTTPResponse("", status=200)
            if url.endswith("/v2/profile/newUploadFile"):
                return _FakeHTTPResponse({"code": 1, "data": {"id": 5555}})
            if url.endswith("/work/operation/sendOrder"):
                return _FakeHTTPResponse({"code": 1, "data": {"status": "ok"}})
            raise AssertionError(f"Unexpected URL: {url}")

        progress_events: list[int] = []
        with tempfile.TemporaryDirectory() as tmp:
            gcode = Path(tmp) / "cube.gcode"
            gcode.write_text("G28\nG1 X10 Y10\n", encoding="utf-8")
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = client.upload_and_print(
                    printer_id=101,
                    file_path=gcode,
                    start_print=True,
                    progress_cb=lambda pct, msg: progress_events.append(pct),
                )

        self.assertEqual(res, {"file_id": 5555, "printer_id": 101, "started": True})
        self.assertEqual([c[0] for c in calls], ["POST", "PUT", "POST", "POST"])
        send_order_body = calls[3][2]
        assert send_order_body is not None
        self.assertEqual(send_order_body["order_id"], 1)
        self.assertEqual(send_order_body["printer_id"], 101)
        self.assertEqual(send_order_body["data"]["file_id"], 5555)
        self.assertEqual(send_order_body["data"]["filetype"], 0)
        self.assertIn(100, progress_events)

    def test_upload_failure_unlocks_storage_space(self) -> None:
        client = AnycubicCloudClient(token="valid_token_1234567890")
        seen_urls: list[str] = []

        def fake_urlopen(req, timeout=30, context=None):
            url = req.full_url
            seen_urls.append(url)
            if url.endswith("/v2/cloud_storage/lockStorageSpace"):
                return _FakeHTTPResponse(
                    {"code": 1, "data": {"id": 777, "preSignUrl": "https://s3.amazonaws.com/fail"}}
                )
            if url.startswith("https://s3.amazonaws.com/"):
                return _FakeHTTPResponse("error", status=500)
            if url.endswith("/v2/cloud_storage/unlockStorageSpace"):
                return _FakeHTTPResponse({"code": 1, "data": {}})
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            gcode = Path(tmp) / "cube.gcode"
            gcode.write_text("G28\n", encoding="utf-8")
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(AnycubicUploadError):
                    client.upload_gcode(gcode)

        self.assertTrue(any(u.endswith("/v2/cloud_storage/unlockStorageSpace") for u in seen_urls))

    def test_print_control_orders(self) -> None:
        client = AnycubicCloudClient(token="valid_token_1234567890")
        bodies: list[dict] = []

        def fake_urlopen(req, timeout=30, context=None):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return _FakeHTTPResponse({"code": 1, "data": {}})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.pause_print(287246, project_id=77)
            client.resume_print(287246, project_id=77)
            client.stop_print(287246, project_id=77)
            client.update_print_settings(287246, project_id=77, settings={"print_speed_mode": 3})

        self.assertEqual([b["order_id"] for b in bodies], [2, 3, 4, 6])
        self.assertEqual(bodies[3]["settings"], {"print_speed_mode": 3})

    def test_gcode_adapter_adds_anycubic_blocks(self) -> None:
        sample = (
            "; HEADER_BLOCK_START\n"
            "; generated by OrcaSlicer 2.5.1 on 2026-11-21\n"
            "; total layer number: 15\n"
            "; max_z_height: 3.04\n"
            "; HEADER_BLOCK_END\n"
            "G28\n"
            "; CONFIG_BLOCK_START\n"
            "; filament_type = PLA\n"
            "; first_layer_bed_temperature = 60\n"
            "; CONFIG_BLOCK_END\n"
            "; filament used [mm] = 3873.40\n"
            "; total layers count = 15\n"
            "; estimated printing time (normal mode) = 22m 53s\n"
        )
        adapted = adapt_gcode_text(sample, model_name="CARDKICK2punto0_v3")
        self.assertIn("; generated by AnycubicSlicerNext 2.0.0.3", adapted)
        self.assertIn("; CONFIG_BLOCK_START = begin", adapted)
        self.assertIn("; AnycubicSlicer_config = begin", adapted)
        self.assertIn("; app_full_name = AnycubicSlicerNext", adapted)
        self.assertIn("; AnycubicSlicer_config = end", adapted)
        self.assertIn("; CONFIG_BLOCK_END = end", adapted)
        self.assertIn("; statistics = begin", adapted)
        self.assertIn("; used_filament = 3.87", adapted)
        self.assertIn("; print_time = 22m 53s", adapted)
        self.assertIn("; total_layers = 15", adapted)
        self.assertIn("G28\n", adapted)

    def test_cloud_files_list_delete_and_print(self) -> None:
        client = AnycubicCloudClient(token="valid_token_1234567890")
        calls: list[tuple[str, str]] = []

        def fake_urlopen(req, timeout=30, context=None):
            calls.append((req.get_method(), req.full_url))
            if req.full_url.endswith("/work/index/files"):
                return _FakeHTTPResponse(
                    {
                        "code": 1,
                        "data": [
                            {
                                "id": 5001,
                                "gcode_id": 9001,
                                "old_filename": "benchy.gcode",
                                "size": 2 * 1024 * 1024,
                                "time": "2026-11-21 16:00:00",
                            }
                        ],
                    }
                )
            if req.full_url.endswith("/work/operation/sendOrder"):
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["data"]["file_id"], 5001)
                self.assertEqual(body["data"]["is_delete_file"], 0)
                return _FakeHTTPResponse({"code": 1, "data": {"msgid": "ok"}})
            if req.full_url.endswith("/work/index/delFiles"):
                body = json.loads(req.data.decode("utf-8"))
                self.assertEqual(body["idArr"], [5001])
                return _FakeHTTPResponse({"code": 1, "data": True})
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            files = client.get_cloud_files()
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "benchy.gcode")
            client.start_print_cloud_file(287246, file_id=5001, gcode_id=9001)
            self.assertTrue(client.delete_cloud_file(5001))

    def test_kvs_presigned_wss_url_format(self) -> None:
        url = build_kvs_presigned_wss_url(
            wss_endpoint="wss://v-12345.kinesisvideo.eu-central-1.amazonaws.com",
            channel_arn="arn:aws:kinesisvideo:eu-central-1:123456789012:channel/test/1",
            client_id="orcacubic-test",
            access_key="AKIATEST",
            secret_key="SECRETTEST",
            session_token="TOKENTEST",
            region="eu-central-1",
        )
        self.assertTrue(url.startswith("wss://v-12345.kinesisvideo.eu-central-1.amazonaws.com/?"))
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)
        self.assertIn("X-Amz-ClientId=orcacubic-test", url)
        self.assertIn("X-Amz-Signature=", url)


class TestOrcaPluginRegistration(unittest.TestCase):
    def test_plugin_registers_capabilities_with_mock_orca(self) -> None:
        registered_caps: list[type] = []
        requested_perms: dict = {}

        mock_orca = types.ModuleType("orca")
        mock_orca.base = type("base", (), {})
        mock_orca.plugin = lambda cls: cls
        mock_orca.register_capability = lambda cls: registered_caps.append(cls)
        def _strict_request_permissions(
            fs_read=(),
            fs_readwrite=(),
            network_http=(),
            network_socket=(),
            process=(),
            threading=False,
            network=(),
        ):
            requested_perms.update(
                {
                    "fs_read": list(fs_read),
                    "fs_readwrite": list(fs_readwrite),
                    "network_http": list(network_http),
                    "network_socket": list(network_socket),
                    "process": list(process),
                    "threading": threading,
                    "network": list(network),
                }
            )

        mock_orca.request_permissions = _strict_request_permissions

        mock_pa = types.ModuleType("orca.printer_agent")

        class _AgentInfo:
            def __init__(self, id="", name="", version="", description=""):
                self.id = id
                self.name = name
                self.version = version
                self.description = description

        mock_pa.AgentInfo = _AgentInfo
        mock_pa.PrinterAgentBase = type("PrinterAgentBase", (), {})
        mock_orca.printer_agent = mock_pa

        mock_pages = types.ModuleType("orca.pages")

        class _PagesBase:
            def __init__(self) -> None:
                self.posted: list = []

            def post_message(self, data) -> None:
                self.posted.append(data)

        mock_pages.PagesPluginCapabilityBase = _PagesBase
        mock_orca.pages = mock_pages

        plugin_path = Path(__file__).resolve().parent.parent / "plugin" / "orcacubic_plugin.py"
        with mock.patch.dict(
            sys.modules,
            {
                "orca": mock_orca,
                "orca.printer_agent": mock_pa,
                "orca.pages": mock_pages,
            },
        ):
            spec = importlib.util.spec_from_file_location("orcacubic_plugin_test", plugin_path)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            pkg = mod.OrcaCubicPlugin()
            pkg.register_capabilities()

            self.assertEqual(len(registered_caps), 2)
            self.assertIn("cloud-universe.anycubic.com", requested_perms["network_http"])
            self.assertTrue(requested_perms["threading"])

            agent = mod.AnycubicPrinterAgent()
            info = agent.get_agent_info()
            self.assertEqual(info.id, "anycubic_cloud")
            self.assertEqual(info.name, "Anycubic Cloud")

            page = mod.AnycubicCloudPage()
            self.assertIn("OrcaCubic", page.get_ui())


if __name__ == "__main__":
    unittest.main()
