"""Anycubic Cloud authentication & request signing (web XX-Token mode)."""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Dict

BASE_DOMAIN = "cloud-universe.anycubic.com"
PUBLIC_API_ROOT = f"https://{BASE_DOMAIN}/p/p/workbench/api"

# Public web client signing parameters used by cloud-universe.anycubic.com
# (verified against WaresWichall/hass-anycubic_cloud)
AC_APP_ID = "f9b3528877c94d5c9c5af32245db46ef"
AC_APP_SECRET = "0cf75926606049a3937f56b0373b99fb"
AC_APP_VERSION_WEB = "1.0.0"
AC_DEVICE_TYPE_WEB = "web"
AC_IS_CN = "2"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 "
    "Safari/537.36"
)


class AnycubicError(Exception):
    """Base error for Anycubic Cloud API."""


class AnycubicAuthError(AnycubicError):
    """Raised when token is missing or rejected."""


class AnycubicTokenExpiredError(AnycubicAuthError):
    """Raised when the Anycubic Cloud token has expired or is invalid."""


class AnycubicUploadError(AnycubicError):
    """Raised when uploading a G-code file to Anycubic Cloud fails."""


class AnycubicAuth:
    """Builds signed Anycubic Cloud HTTP headers for a given XX-Token."""

    def __init__(
        self,
        token: str,
        app_id: str = AC_APP_ID,
        app_secret: str = AC_APP_SECRET,
        app_version: str = AC_APP_VERSION_WEB,
        device_type: str = AC_DEVICE_TYPE_WEB,
        is_cn: str = AC_IS_CN,
    ) -> None:
        self._token = (token or "").strip()
        self._app_id = app_id
        self._app_secret = app_secret
        self._app_version = app_version
        self._device_type = device_type
        self._is_cn = is_cn

    @property
    def token(self) -> str:
        if not self._token:
            raise AnycubicAuthError(
                "Anycubic Cloud token is not set. Sign in or paste your XX-Token."
            )
        return self._token

    def build_headers(
        self,
        with_token: bool = True,
        timestamp_ms: int | None = None,
        nonce: str | None = None,
    ) -> Dict[str, str]:
        ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
        req_nonce = str(uuid.uuid1()) if nonce is None else nonce
        sig_input = (
            f"{self._app_id}{ts}{self._app_version}"
            f"{self._app_secret}{req_nonce}{self._app_id}"
        )
        signature = hashlib.md5(sig_input.encode("utf-8")).hexdigest()

        headers: Dict[str, str] = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": f"https://{BASE_DOMAIN}",
            "Content-Type": "application/json",
            "Xx-Device-Type": self._device_type,
            "Xx-Is-Cn": self._is_cn,
            "Xx-Nonce": req_nonce,
            "Xx-Signature": signature,
            "Xx-Timestamp": str(ts),
            "Xx-Version": self._app_version,
            "XX-LANGUAGE": "US",
        }
        if with_token:
            headers["XX-Token"] = self.token
        return headers
