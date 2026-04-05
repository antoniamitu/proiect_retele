from __future__ import annotations

import json
import threading
import time
from typing import Callable

from client.src.local_state import LocalState
from shared.src.config import RETRY_INTERVAL


def process_pending_updates(
    local_state: LocalState,
    log_func: Callable[[str], None] = print,
) -> None:
    """
    Executes one retry pass:
    1. removes orphaned .meta files
    2. removes orphaned .new files
    3. applies valid pending updates when unlocked
    """

    # First pass: remove orphaned .meta files
    for app_name in local_state.list_pending_meta_app_names():
        if not local_state.pending_new_exists(app_name):
            log_func(
                f"[RETRY] WARNING: {app_name}.meta found without .new, deleting orphaned metadata"
            )
            local_state.delete_orphan_pending_meta(app_name)

    # Second pass: process .new files
    for app_name in local_state.list_pending_app_names():
        if not local_state.pending_meta_exists(app_name):
            log_func(
                f"[RETRY] ERROR: {app_name}.new found without .meta, deleting orphaned binary"
            )
            local_state.delete_orphan_pending_binary(app_name)
            continue

        if local_state.is_app_locked(app_name):
            log_func(f"[RETRY] {app_name} still locked, retrying...")
            continue

        try:
            metadata = local_state.read_pending_metadata(app_name)
            version = metadata["version"]
            hash_value = metadata["hash"]

            if not isinstance(version, int):
                raise ValueError(f"Invalid version in metadata for {app_name}")
            if not isinstance(hash_value, str):
                raise ValueError(f"Invalid hash in metadata for {app_name}")

        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            FileNotFoundError,
        ):
            log_func(
                f"[RETRY] ERROR: invalid metadata for {app_name}, deleting corrupted pending update"
            )
            local_state.delete_orphan_pending_binary(app_name)
            local_state.delete_orphan_pending_meta(app_name)
            continue

        success = local_state.apply_pending_update(app_name, version, hash_value)
        if success:
            log_func(f"[RETRY] Applied update for {app_name}")


def retry_worker_loop(
    local_state: LocalState,
    stop_event: threading.Event | None = None,
    log_func: Callable[[str], None] = print,
) -> None:
    """
    Background daemon loop.
    Runs forever unless a stop_event is provided and set.
    """
    while True:
        if stop_event is not None and stop_event.is_set():
            return

        process_pending_updates(local_state, log_func=log_func)

        if stop_event is not None:
            if stop_event.wait(RETRY_INTERVAL):
                return
        else:
            time.sleep(RETRY_INTERVAL)


def start_retry_worker(
    local_state: LocalState,
    stop_event: threading.Event | None = None,
    log_func: Callable[[str], None] = print,
) -> threading.Thread:
    thread = threading.Thread(
        target=retry_worker_loop,
        args=(local_state, stop_event, log_func),
        daemon=True,
        name="retry-worker",
    )
    thread.start()
    return thread