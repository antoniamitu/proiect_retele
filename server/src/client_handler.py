# server/src/client_handler.py
from __future__ import annotations

import select
import socket as socket_module
import time
from typing import Any

from shared.src.config import ACK_TIMEOUT, FRAME_TIMEOUT, SOCKET_TIMEOUT
from shared.src.protocol import (
    ACK,
    APP_NOT_FOUND,
    CHECK_UPDATES,
    CHECK_UPDATES_RESPONSE,
    CLIENT_NOT_REGISTERED,
    DISCONNECT,
    DOWNLOAD,
    FILE_TRANSFER,
    HELLO,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    LIST_APPS,
    LIST_RESPONSE,
    MISSING_FIELD,
    PUSH_UPDATE,
    MissingFieldError,
    ProtocolFrameError,
    build_ok,
    is_valid_app_name,
    recv_message,
    require_field,
    send_error,
    send_message,
)
from server.src.state_manager import StateManager


def log(message: str) -> None:
    print(f"[SERVER] {message}")


def _send_list_apps(
    sock: socket_module.socket,
    request_id: int,
    state_manager: StateManager,
) -> None:
    with state_manager.lock:
        apps = [
            {
                "name": app_name,
                "version": record["version"],
                "hash": record["hash"],
            }
            for app_name, record in sorted(state_manager.apps.items())
        ]

    response = build_ok(LIST_RESPONSE, request_id, apps=apps)
    send_message(sock, response)


def _send_download(
    sock: socket_module.socket,
    request_id: int,
    app_name: str,
    state_manager: StateManager,
) -> dict[str, Any]:
    record = state_manager.get_app_record(app_name)
    if record is None:
        raise FileNotFoundError(app_name)

    file_data = state_manager.read_app_binary(app_name)
    if file_data is None:
        raise FileNotFoundError(app_name)

    header = build_ok(
        FILE_TRANSFER,
        request_id,
        app_name=app_name,
        version=record["version"],
        hash=record["hash"],
    )
    send_message(sock, header, file_data)

    return {
        "request_id": request_id,
        "kind": "DOWNLOAD",
        "app_name": app_name,
        "version": record["version"],
        "since": time.time(),
    }


def _build_updates_response(
    client_id: str,
    local_apps: list[dict[str, Any]],
    state_manager: StateManager,
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    known_local_apps: list[str] = []

    with state_manager.lock:
        server_apps_snapshot = {
            app_name: dict(record)
            for app_name, record in state_manager.apps.items()
        }

    for item in local_apps:
        if not isinstance(item, dict):
            raise ValueError("Each item in local_apps must be a dict")

        if "name" not in item:
            raise MissingFieldError("name")
        if "version" not in item:
            raise MissingFieldError("version")

        app_name = item["name"]
        local_version = item["version"]

        if not is_valid_app_name(app_name):
            raise ValueError(f"Invalid app_name in local_apps: {app_name!r}")
        if not isinstance(local_version, int):
            raise ValueError(f"Invalid version in local_apps for {app_name!r}")

        if app_name in server_apps_snapshot:
            known_local_apps.append(app_name)
            server_record = server_apps_snapshot[app_name]
            if server_record["version"] > local_version:
                updates.append(
                    {
                        "name": app_name,
                        "version": server_record["version"],
                        "hash": server_record["hash"],
                    }
                )

    state_manager.resync_client_downloads(client_id, known_local_apps)
    return updates


def _handle_hello(
    sock: socket_module.socket,
    header: dict[str, Any],
    state_manager: StateManager,
) -> str:
    request_id = header["request_id"]

    client_id = require_field(header, "client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id must be a non-empty string")

    old_socket = state_manager.set_active_client(client_id, sock)

    if old_socket is not None and old_socket is not sock:
        try:
            old_socket.close()
        except OSError:
            pass

    response = build_ok(
        HELLO,
        request_id,
        message=f"Welcome, {client_id}",
    )
    send_message(sock, response)
    log(f"Client connected as {client_id}")
    return client_id


def _send_internal_error(
    sock: socket_module.socket,
    header: dict[str, Any],
    exc: Exception,
    *,
    context: str,
) -> bool:
    log(f"Internal error during {context}: {exc}")
    try:
        send_error(sock, header, INTERNAL_ERROR, "Unexpected server failure")
    except OSError:
        return False
    return True


def handle_client(
    sock: socket_module.socket,
    addr,
    state_manager: StateManager,
) -> None:
    client_id: str | None = None
    pending_transfer: dict[str, Any] | None = None

    try:
        while True:
            if pending_transfer is not None:
                if time.time() - pending_transfer["since"] > ACK_TIMEOUT:
                    log(f"ACK timeout for {client_id}, closing connection")
                    break

            try:
                readable, _, _ = select.select([sock], [], [], SOCKET_TIMEOUT)
            except OSError:
                break

            if readable:
                try:
                    sock.settimeout(FRAME_TIMEOUT)
                    header, file_data = recv_message(sock)
                except ProtocolFrameError as exc:
                    log(f"Protocol frame error from {client_id}: {exc}. Closing connection.")
                    break
                except socket_module.timeout:
                    log(f"Incomplete frame from {client_id}, closing connection")
                    break
                except (ConnectionError, ConnectionResetError, OSError):
                    break
                finally:
                    try:
                        sock.settimeout(None)
                    except OSError:
                        pass

                if "request_id" not in header:
                    try:
                        send_error(sock, header, MISSING_FIELD, "request_id is required")
                    except OSError:
                        break
                    continue

                if not isinstance(header["request_id"], int):
                    try:
                        send_error(sock, header, INVALID_REQUEST, "request_id must be an integer")
                    except OSError:
                        break
                    continue

                action = header.get("action")

                if pending_transfer is not None:
                    if action != ACK or header.get("ack_for_request_id") != pending_transfer["request_id"]:
                        try:
                            send_error(sock, header, INVALID_REQUEST, "Expected ACK, protocol violation")
                        except OSError:
                            pass
                        break

                    if pending_transfer["kind"] == "DOWNLOAD" and client_id is not None:
                        try:
                            state_manager.register_download(client_id, pending_transfer["app_name"])
                        except Exception as exc:
                            if not _send_internal_error(
                                sock,
                                header,
                                exc,
                                context="ACK download registration",
                            ):
                                break

                    pending_transfer = None
                    continue

                if action is None:
                    try:
                        send_error(sock, header, MISSING_FIELD, "action is required")
                    except OSError:
                        break
                    continue

                if client_id is None and action != HELLO:
                    try:
                        send_error(
                            sock,
                            header,
                            CLIENT_NOT_REGISTERED,
                            "HELLO must be sent before any other action",
                        )
                    except OSError:
                        break
                    continue

                if action == ACK:
                    try:
                        send_error(sock, header, INVALID_REQUEST, "No transfer pending")
                    except OSError:
                        break
                    continue

                if action == HELLO:
                    if client_id is not None:
                        try:
                            send_error(sock, header, INVALID_REQUEST, "HELLO already completed")
                        except OSError:
                            break
                        continue

                    try:
                        client_id = _handle_hello(sock, header, state_manager)
                    except MissingFieldError as exc:
                        try:
                            send_error(sock, header, MISSING_FIELD, f"Missing field: {exc}")
                        except OSError:
                            break
                    except ValueError as exc:
                        try:
                            send_error(sock, header, INVALID_REQUEST, str(exc))
                        except OSError:
                            break
                    except OSError:
                        break
                    except Exception as exc:
                        if not _send_internal_error(sock, header, exc, context="HELLO"):
                            break
                    continue

                if action == LIST_APPS:
                    try:
                        _send_list_apps(sock, header["request_id"], state_manager)
                    except OSError:
                        break
                    except Exception as exc:
                        if not _send_internal_error(sock, header, exc, context="LIST_APPS"):
                            break
                    continue

                if action == DOWNLOAD:
                    try:
                        app_name = require_field(header, "app_name")
                    except MissingFieldError as exc:
                        try:
                            send_error(sock, header, MISSING_FIELD, f"Missing field: {exc}")
                        except OSError:
                            break
                        continue

                    if not is_valid_app_name(app_name):
                        try:
                            send_error(sock, header, INVALID_REQUEST, "Invalid app_name")
                        except OSError:
                            break
                        continue

                    if not state_manager.app_exists(app_name):
                        try:
                            send_error(sock, header, APP_NOT_FOUND, f"Application '{app_name}' does not exist")
                        except OSError:
                            break
                        continue

                    try:
                        pending_transfer = _send_download(
                            sock,
                            header["request_id"],
                            app_name,
                            state_manager,
                        )
                    except FileNotFoundError:
                        try:
                            send_error(sock, header, APP_NOT_FOUND, f"Application '{app_name}' does not exist")
                        except OSError:
                            break
                    except OSError:
                        break
                    except Exception as exc:
                        if not _send_internal_error(sock, header, exc, context="DOWNLOAD"):
                            break
                    continue

                if action == CHECK_UPDATES:
                    try:
                        local_apps = require_field(header, "local_apps")
                    except MissingFieldError as exc:
                        try:
                            send_error(sock, header, MISSING_FIELD, f"Missing field: {exc}")
                        except OSError:
                            break
                        continue

                    if not isinstance(local_apps, list):
                        try:
                            send_error(sock, header, INVALID_REQUEST, "local_apps must be a list")
                        except OSError:
                            break
                        continue

                    try:
                        assert client_id is not None
                        updates = _build_updates_response(client_id, local_apps, state_manager)
                        response = build_ok(
                            CHECK_UPDATES_RESPONSE,
                            header["request_id"],
                            updates=updates,
                        )
                        send_message(sock, response)
                    except MissingFieldError as exc:
                        try:
                            send_error(sock, header, MISSING_FIELD, f"Missing field in local_apps item: {exc}")
                        except OSError:
                            break
                    except ValueError as exc:
                        try:
                            send_error(sock, header, INVALID_REQUEST, str(exc))
                        except OSError:
                            break
                    except OSError:
                        break
                    except Exception as exc:
                        if not _send_internal_error(sock, header, exc, context="CHECK_UPDATES"):
                            break
                    continue

                if action == DISCONNECT:
                    break

                try:
                    send_error(sock, header, INVALID_REQUEST, f"Unknown action: {action}")
                except OSError:
                    break

            if not readable and pending_transfer is None and client_id is not None:
                task = state_manager.pop_next_push(client_id)
                if task is not None:
                    push_header = build_ok(
                        PUSH_UPDATE,
                        task.request_id,
                        app_name=task.app_name,
                        version=task.version,
                        hash=task.hash_value,
                    )

                    try:
                        send_message(sock, push_header, task.file_data)
                    except (ConnectionError, ConnectionResetError, OSError):
                        break

                    pending_transfer = {
                        "request_id": task.request_id,
                        "kind": "PUSH_UPDATE",
                        "app_name": task.app_name,
                        "version": task.version,
                        "since": time.time(),
                    }

    finally:
        state_manager.cleanup_client_if_current(client_id, sock)
        log(f"Connection closed for {client_id or addr}")