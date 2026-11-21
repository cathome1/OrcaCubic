"""Pure-stdlib AWS Kinesis Video Streams (WebRTC) viewer session for Anycubic Cloud cameras."""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

from .auth import AnycubicError
from .client import AnycubicCloudClient

ORDER_ID_CAMERA_OPEN = 1001
ORDER_ID_CAMERA_CLOSE = 1002


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def _sigv4_post(
    url: str,
    body_dict: Dict[str, Any],
    access_key: str,
    secret_key: str,
    session_token: str,
    region: str,
    service: str = "kinesisvideo",
    ssl_context: Optional[ssl.SSLContext] = None,
) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    uri = parsed.path or "/"
    body = json.dumps(body_dict).encode("utf-8")

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{session_token}\n"
    )
    signed_headers = "host;x-amz-date;x-amz-security-token"
    canonical_request = (
        f"POST\n{uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _sigv4_signing_key(secret_key, date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    req = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Amz-Date": amz_date,
            "X-Amz-Security-Token": session_token,
            "Authorization": auth_header,
        },
    )
    ctx = ssl_context or ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404 or "ResourceNotFound" in err_body:
            raise AnycubicError(
                "Канал камеры не найден. Проверьте, что USB-камера подключена к принтеру."
            ) from exc
        raise AnycubicError(
            f"Ошибка AWS Kinesis Video (HTTP {exc.code})."
        ) from exc
    except urllib.error.URLError as exc:
        raise AnycubicError(f"Нет связи с сервером видеопотока: {exc.reason}") from exc


def build_kvs_presigned_wss_url(
    wss_endpoint: str,
    channel_arn: str,
    client_id: str,
    access_key: str,
    secret_key: str,
    session_token: str,
    region: str,
    now_utc: Optional[datetime.datetime] = None,
) -> str:
    """Create an AWS SigV4 presigned WebSocket URL for KVS WebRTC Viewer."""
    parsed = urllib.parse.urlparse(wss_endpoint)
    host = parsed.netloc
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    service = "kinesisvideo"
    scope = f"{date_stamp}/{region}/{service}/aws4_request"

    query_params = [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
        ("X-Amz-ChannelARN", channel_arn),
        ("X-Amz-ClientId", client_id),
        ("X-Amz-Credential", f"{access_key}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", "299"),
        ("X-Amz-Security-Token", session_token),
        ("X-Amz-SignedHeaders", "host"),
    ]
    query_params.sort(key=lambda kv: kv[0])
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in query_params
    )
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    empty_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = (
        f"GET\n/\n{canonical_qs}\n{canonical_headers}\n{signed_headers}\n{empty_hash}"
    )
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )
    signing_key = _sigv4_signing_key(secret_key, date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return f"wss://{host}/?{canonical_qs}&X-Amz-Signature={signature}"


def open_camera_session(client: AnycubicCloudClient, printer_id: int) -> Dict[str, Any]:
    """Open Anycubic Cloud camera and return WebRTC signaling URL + ICE servers."""
    info = client._request_json("GET", "/v2/printer/info", query={"id": str(printer_id)})
    info_data = info.get("data") or {}
    printer_key = str(info_data.get("key") or "").strip()
    if not printer_key:
        raise AnycubicError("Не удалось получить ключ принтера для подключения камеры.")

    cam_resp = client._request_json(
        "POST",
        "/work/operation/sendOrder",
        body={
            "order_id": ORDER_ID_CAMERA_OPEN,
            "printer_id": int(printer_id),
        },
    )
    cam_data = cam_resp.get("data") or {}
    tok = cam_data.get("token")
    if not isinstance(tok, dict) or "tmpSecretId" not in tok:
        raise AnycubicError(
            "Принтер не вернул AWS-токен камеры (возможно, камера не подключена или используется другой протокол)."
        )

    region = str(tok.get("region") or "eu-central-1")
    ak = str(tok["tmpSecretId"])
    sk = str(tok["tmpSecretKey"])
    st = str(tok["sessionToken"])

    kvs_base = f"https://kinesisvideo.{region}.amazonaws.com"
    desc = _sigv4_post(
        f"{kvs_base}/describeSignalingChannel",
        {"ChannelName": printer_key},
        ak,
        sk,
        st,
        region,
    )
    channel_arn = str((desc.get("ChannelInfo") or {}).get("ChannelARN") or "")
    if not channel_arn:
        raise AnycubicError("Не удалось получить ChannelARN камеры.")

    ep_resp = _sigv4_post(
        f"{kvs_base}/getSignalingChannelEndpoint",
        {
            "ChannelARN": channel_arn,
            "SingleMasterChannelEndpointConfiguration": {
                "Protocols": ["WSS", "HTTPS"],
                "Role": "VIEWER",
            },
        },
        ak,
        sk,
        st,
        region,
    )
    endpoints = {
        e["Protocol"]: e["ResourceEndpoint"]
        for e in ep_resp.get("ResourceEndpointList", [])
        if isinstance(e, dict) and "Protocol" in e and "ResourceEndpoint" in e
    }
    wss_ep = endpoints.get("WSS")
    https_ep = endpoints.get("HTTPS")
    if not wss_ep or not https_ep:
        raise AnycubicError("AWS Kinesis Video не вернул WSS/HTTPS эндпоинты.")

    ice_resp = _sigv4_post(
        f"{https_ep}/v1/get-ice-server-config",
        {"ChannelARN": channel_arn},
        ak,
        sk,
        st,
        region,
    )
    ice_servers: List[Dict[str, Any]] = [
        {"urls": f"stun:stun.kinesisvideo.{region}.amazonaws.com:443"}
    ]
    for srv in ice_resp.get("IceServerList", []):
        if isinstance(srv, dict) and srv.get("Uris"):
            entry: Dict[str, Any] = {"urls": srv["Uris"]}
            if srv.get("Username"):
                entry["username"] = srv["Username"]
            if srv.get("Password"):
                entry["credential"] = srv["Password"]
            ice_servers.append(entry)

    client_id = f"orcacubic-{uuid.uuid4().hex[:12]}"
    wss_url = build_kvs_presigned_wss_url(
        wss_endpoint=wss_ep,
        channel_arn=channel_arn,
        client_id=client_id,
        access_key=ak,
        secret_key=sk,
        session_token=st,
        region=region,
    )
    return {
        "wss_url": wss_url,
        "ice_servers": ice_servers,
        "client_id": client_id,
        "region": region,
    }


def close_camera_session(client: AnycubicCloudClient, printer_id: int) -> None:
    """Best-effort CAMERA_CLOSE order."""
    try:
        client._request_json(
            "POST",
            "/work/operation/sendOrder",
            body={
                "order_id": ORDER_ID_CAMERA_CLOSE,
                "printer_id": int(printer_id),
                "project_id": 0,
                "data": None,
            },
        )
    except AnycubicError:
        pass
