from __future__ import annotations

import json
import socket
import struct
from typing import Any

from shared.src.config import BUFFER_SIZE, ENCODING

# Client -> Server
HELLO = "HELLO"
LIST_APPS = "LIST_APPS"
DOWNLOAD = "DOWNLOAD"
CHECK_UPDATES = "CHECK_UPDATES"
ACK = "ACK"
DISCONNECT = "DISCONNECT"

# Server -> Client
LIST_RESPONSE = "LIST_RESPONSE"
FILE_TRANSFER = "FILE_TRANSFER"
PUSH_UPDATE = "PUSH_UPDATE"
CHECK_UPDATES_RESPONSE = "CHECK_UPDATES_RESPONSE"
ERROR = "ERROR"

# Error codes
INVALID_REQUEST = "INVALID_REQUEST"
MISSING_FIELD = "MISSING_FIELD"
APP_NOT_FOUND = "APP_NOT_FOUND"
CLIENT_NOT_REGISTERED = "CLIENT_NOT_REGISTERED"
INTERNAL_ERROR = "INTERNAL_ERROR"


class ProtocolFrameError(Exception):
    """Raised when the incoming frame is structurally invalid."""


class MissingFieldError(Exception):
    """Raised when a required protocol field is missing."""


def send_message(
    sock: socket.socket,
    header_dict: dict[str, Any],
    file_data: bytes | None = None,
) -> None:
    header = dict(header_dict)  # shallow copy, never mutate caller's dict

    if file_data is not None:
        header["file_size"] = len(file_data)
    else:
        header.pop("file_size", None)

    json_bytes = json.dumps(header).encode(ENCODING)

    sock.sendall(struct.pack("!I", len(json_bytes)))
    sock.sendall(json_bytes)

    if file_data is not None:
        sock.sendall(file_data)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()

    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), BUFFER_SIZE))
        if not chunk:
            raise ConnectionError("Socket closed")
        buf.extend(chunk)

    return bytes(buf)


def recv_message(sock: socket.socket) -> tuple[dict[str, Any], bytes | None]:
    raw_length = recv_exact(sock, 4)
    json_length = struct.unpack("!I", raw_length)[0]

    json_bytes = recv_exact(sock, json_length)

    try:
        header = json.loads(json_bytes.decode(ENCODING))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolFrameError("Invalid JSON frame") from exc

    if not isinstance(header, dict):
        raise ProtocolFrameError("Decoded header is not a dict")

    file_size = header.get("file_size", 0)
    if not isinstance(file_size, int) or file_size < 0:
        raise ProtocolFrameError("file_size must be a non-negative integer")

    file_data = None
    if file_size > 0:
        file_data = recv_exact(sock, file_size)

    return header, file_data


def get_safe_request_id(header: Any) -> int:
    if isinstance(header, dict):
        request_id = header.get("request_id")
        if isinstance(request_id, int):
            return request_id
    return 0


def build_ok(action: str, request_id: int, **payload: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": "OK",
        "action": action,
        "request_id": request_id,
    }
    response.update(payload)
    return response


def build_error(request_id: int, code: str, message: str) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "action": ERROR,
        "request_id": request_id,
        "code": code,
        "message": message,
    }


def send_error(
    sock: socket.socket,
    received_header: Any,
    code: str,
    message: str,
) -> None:
    request_id = get_safe_request_id(received_header)
    send_message(sock, build_error(request_id, code, message))


def require_field(header: dict[str, Any], field_name: str) -> Any:
    if field_name not in header:
        raise MissingFieldError(field_name)
    return header[field_name]


def is_valid_app_name(app_name: Any) -> bool:
    if not isinstance(app_name, str):
        return False
    if not app_name.strip():
        return False
    if "/" in app_name or "\\" in app_name or ".." in app_name:
        return False
    return True