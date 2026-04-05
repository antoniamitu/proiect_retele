from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared.src.config import (
    APPS_MANIFEST_PATH,
    DOWNLOADS_REGISTRY_PATH,
    ENCODING,
    JSON_INDENT,
    SERVER_APPS_DIR,
    SERVER_DATA_DIR,
)


@dataclass(slots=True)
class PushTask:
    request_id: int
    app_name: str
    version: int
    hash_value: str
    file_data: bytes


class StateManager:
    def __init__(self) -> None:
        # RLock avoids accidental self-deadlocks if one public method is called
        # from another lock-protected state_manager flow.
        self.lock = threading.RLock()

        # Persistent: app_name -> {"name", "version", "hash", "file_path"}
        self.apps: dict[str, dict[str, Any]] = {}

        # Persistent: client_id -> set of app_names
        self.downloads: dict[str, set[str]] = {}

        # Live only: client_id -> socket object
        self.active_clients: dict[str, socket.socket] = {}

        # Live only: client_id -> dict[app_name] -> PushTask
        self.pending_pushes: dict[str, dict[str, PushTask]] = {}

        # Server-side request_id counter for PUSH_UPDATE messages
        self.server_request_id = 1000

        self._ensure_storage()
        self.load_from_disk()

    def _ensure_storage(self) -> None:
        SERVER_APPS_DIR.mkdir(parents=True, exist_ok=True)
        SERVER_DATA_DIR.mkdir(parents=True, exist_ok=True)

        if not APPS_MANIFEST_PATH.exists():
            APPS_MANIFEST_PATH.write_text("{}", encoding=ENCODING)

        if not DOWNLOADS_REGISTRY_PATH.exists():
            DOWNLOADS_REGISTRY_PATH.write_text("{}", encoding=ENCODING)

    def _write_json_atomic(self, path: Path, payload: Any) -> None:
        # If a leftover .tmp exists from a previous interrupted write, write_text
        # simply overwrites it, then os.replace() atomically swaps it in place.
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=JSON_INDENT),
            encoding=ENCODING,
        )
        os.replace(tmp_path, path)

    def load_from_disk(self) -> None:
        with self.lock:
            self.apps = self._load_apps_manifest()
            self.downloads = self._load_downloads_registry()

    def _load_apps_manifest(self) -> dict[str, dict[str, Any]]:
        try:
            content = json.loads(APPS_MANIFEST_PATH.read_text(encoding=ENCODING))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

        if not isinstance(content, dict):
            return {}

        result: dict[str, dict[str, Any]] = {}
        for app_name, record in content.items():
            if not isinstance(app_name, str) or not isinstance(record, dict):
                continue

            name = record.get("name", app_name)
            version = record.get("version")
            hash_value = record.get("hash")
            file_path = record.get("file_path", app_name)

            if (
                isinstance(name, str)
                and isinstance(version, int)
                and version >= 1
                and isinstance(hash_value, str)
                and isinstance(file_path, str)
            ):
                result[app_name] = {
                    "name": name,
                    "version": version,
                    "hash": hash_value,
                    "file_path": file_path,
                }

        return result

    def _load_downloads_registry(self) -> dict[str, set[str]]:
        try:
            content = json.loads(DOWNLOADS_REGISTRY_PATH.read_text(encoding=ENCODING))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

        if not isinstance(content, dict):
            return {}

        result: dict[str, set[str]] = {}
        for client_id, app_names in content.items():
            if isinstance(client_id, str) and isinstance(app_names, list):
                result[client_id] = {
                    app_name
                    for app_name in app_names
                    if isinstance(app_name, str)
                }

        return result

    def _save_apps_manifest_unlocked(self) -> None:
        self._write_json_atomic(APPS_MANIFEST_PATH, self.apps)

    def save_apps_manifest(self) -> None:
        with self.lock:
            self._save_apps_manifest_unlocked()

    def _save_downloads_registry_unlocked(self) -> None:
        serializable = {
            client_id: sorted(app_names)
            for client_id, app_names in self.downloads.items()
        }
        self._write_json_atomic(DOWNLOADS_REGISTRY_PATH, serializable)

    def save_downloads_registry(self) -> None:
        with self.lock:
            self._save_downloads_registry_unlocked()

    def get_next_server_request_id(self) -> int:
        with self.lock:
            self.server_request_id += 1
            return self.server_request_id

    def set_active_client(
        self,
        client_id: str,
        sock: socket.socket,
    ) -> socket.socket | None:
        with self.lock:
            old_socket = self.active_clients.get(client_id)
            self.active_clients[client_id] = sock
            self.pending_pushes.setdefault(client_id, {})
            self.downloads.setdefault(client_id, set())
            return old_socket

    def cleanup_client_if_current(
        self,
        client_id: str | None,
        sock: socket.socket,
    ) -> None:
        if client_id is None:
            sock.close()
            return

        with self.lock:
            if self.active_clients.get(client_id) is sock:
                del self.active_clients[client_id]
                self.pending_pushes.pop(client_id, None)

        sock.close()

    def get_app_record(self, app_name: str) -> dict[str, Any] | None:
        with self.lock:
            record = self.apps.get(app_name)
            return dict(record) if record is not None else None

    def app_exists(self, app_name: str) -> bool:
        with self.lock:
            return app_name in self.apps

    def register_download(self, client_id: str, app_name: str) -> None:
        with self.lock:
            self.downloads.setdefault(client_id, set()).add(app_name)
            self._save_downloads_registry_unlocked()

    def resync_client_downloads(self, client_id: str, app_names: list[str]) -> bool:
        changed = False

        with self.lock:
            current = self.downloads.setdefault(client_id, set())

            for app_name in app_names:
                if app_name in self.apps and app_name not in current:
                    current.add(app_name)
                    changed = True

            if changed:
                self._save_downloads_registry_unlocked()

        return changed

    def update_app_record(self, app_name: str, version: int, hash_value: str) -> None:
        with self.lock:
            self.apps[app_name] = {
                "name": app_name,
                "version": version,
                "hash": hash_value,
                "file_path": app_name,
            }
            self._save_apps_manifest_unlocked()

    def enqueue_push(self, client_id: str, task: PushTask) -> None:
        with self.lock:
            client_pushes = self.pending_pushes.setdefault(client_id, {})
            client_pushes[task.app_name] = task

    def pop_next_push(self, client_id: str) -> PushTask | None:
        with self.lock:
            pushes = self.pending_pushes.get(client_id, {})
            if not pushes:
                return None

            app_name = next(iter(pushes))
            return pushes.pop(app_name)

    def get_clients_who_downloaded(self, app_name: str) -> list[str]:
        with self.lock:
            return [
                client_id
                for client_id, app_names in self.downloads.items()
                if app_name in app_names
            ]

    def read_app_binary(self, app_name: str) -> bytes | None:
        with self.lock:
            record = self.apps.get(app_name)
            if record is None:
                return None
            file_path = SERVER_APPS_DIR / record["file_path"]

        if not file_path.exists():
            return None

        return file_path.read_bytes()

    def get_connected_clients_for_app(self, app_name: str) -> list[str]:
        with self.lock:
            return [
                client_id
                for client_id, app_names in self.downloads.items()
                if app_name in app_names and client_id in self.active_clients
            ]