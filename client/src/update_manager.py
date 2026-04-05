from __future__ import annotations

from typing import Any, Callable

from client.src.local_state import LocalState
from shared.src.hash_utils import compute_hash
from shared.src.protocol import (
    ACK,
    FILE_TRANSFER,
    PUSH_UPDATE,
    MissingFieldError,
    is_valid_app_name,
    require_field,
)


def process_received_file(
    local_state: LocalState,
    header: dict[str, Any],
    file_data: bytes | None,
    log_func: Callable[[str], None] = print,
) -> bool:
    """
    Handles a received FILE_TRANSFER or PUSH_UPDATE.

    Returns True only if:
    - the header is valid
    - the binary payload exists
    - the hash matches
    - the file is successfully saved

    If True is returned, the caller is allowed to send ACK.
    """
    if file_data is None:
        log_func("[CLIENT] ERROR: received transfer without binary payload")
        return False

    try:
        action = require_field(header, "action")
        app_name = require_field(header, "app_name")
        version = require_field(header, "version")
        hash_value = require_field(header, "hash")
    except MissingFieldError as exc:
        log_func(f"[CLIENT] ERROR: missing field in transfer header: {exc}")
        return False

    if action not in {FILE_TRANSFER, PUSH_UPDATE}:
        log_func(f"[CLIENT] ERROR: unexpected transfer action: {action}")
        return False

    if not is_valid_app_name(app_name):
        log_func(f"[CLIENT] ERROR: invalid app_name in transfer: {app_name!r}")
        return False

    if not isinstance(version, int) or version < 1:
        log_func(f"[CLIENT] ERROR: invalid version for {app_name}")
        return False

    if not isinstance(hash_value, str) or not hash_value:
        log_func(f"[CLIENT] ERROR: invalid hash for {app_name}")
        return False

    actual_hash = compute_hash(file_data)
    if actual_hash != hash_value:
        log_func(
            f"[CLIENT] ERROR: hash mismatch for {app_name}. expected={hash_value} got={actual_hash}"
        )
        return False

    try:
        if local_state.is_app_locked(app_name):
            local_state.write_pending_update(app_name, file_data, version, hash_value)
            log_func(
                f"[CLIENT] Stored pending update for {app_name} v{version} (app is locked)"
            )
        else:
            local_state.write_installed_file(app_name, file_data, version, hash_value)
            log_func(f"[CLIENT] Installed {app_name} v{version}")
    except OSError as exc:
        log_func(f"[CLIENT] ERROR: failed to save {app_name}: {exc}")
        return False

    return True


def build_ack_message(
    request_id: int,
    ack_for_request_id: int,
    app_name: str,
    version: int,
) -> dict[str, Any]:
    return {
        "action": ACK,
        "request_id": request_id,
        "ack_for_request_id": ack_for_request_id,
        "app_name": app_name,
        "version": version,
    }