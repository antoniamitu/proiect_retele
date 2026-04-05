# server/src/publisher.py
from __future__ import annotations

from typing import Any

from shared.src.config import SERVER_APPS_DIR
from shared.src.hash_utils import compute_hash
from shared.src.protocol import is_valid_app_name
from server.src.state_manager import PushTask, StateManager


def publish_app(state_manager: StateManager, app_name: str) -> dict[str, Any]:
    """
    Publishes a new version of an already registered app file from server/apps/.

    The operator must overwrite the existing file on disk first, then call publish.
    Structural changes to the app list are not allowed at runtime.
    Returns a small summary dict for logging/UI.
    """
    if not is_valid_app_name(app_name):
        raise ValueError(f"Invalid app name: {app_name!r}")

    app_path = SERVER_APPS_DIR / app_name
    if not app_path.exists() or not app_path.is_file():
        raise FileNotFoundError(f"App file does not exist on disk: {app_name}")

    file_data = app_path.read_bytes()
    hash_value = compute_hash(file_data)

    with state_manager.lock:
        old_record = state_manager.get_app_record(app_name)
        if old_record is None:
            raise ValueError(
                f"App '{app_name}' is not registered in apps_manifest.json. "
                "Runtime publish only supports new versions of existing apps."
            )

        version = int(old_record["version"]) + 1

        # Use StateManager API instead of mutating internals directly.
        # RLock makes this safe even though these methods also acquire the lock.
        state_manager.update_app_record(app_name, version, hash_value)

        queued_for_clients: list[str] = []
        connected_client_ids = state_manager.get_connected_clients_for_app(app_name)

        for client_id in connected_client_ids:
            request_id = state_manager.get_next_server_request_id()

            push_task = PushTask(
                request_id=request_id,
                app_name=app_name,
                version=version,
                hash_value=hash_value,
                file_data=file_data,
            )

            state_manager.enqueue_push(client_id, push_task)
            queued_for_clients.append(client_id)

    queued_for_clients.sort()

    return {
        "app_name": app_name,
        "version": version,
        "hash": hash_value,
        "queued_for_clients": queued_for_clients,
    }