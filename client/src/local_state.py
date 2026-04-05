from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from shared.src.config import (
    CLIENT_DIR,
    CLIENT_STATE_PATH,
    ENCODING,
    JSON_INDENT,
    build_client_paths,
)


class LocalState:
    def __init__(self, client_id: str, base_dir: Path | None = None) -> None:
        self.client_id = client_id
        self.base_dir = Path(base_dir) if base_dir is not None else CLIENT_DIR

        paths = build_client_paths(self.base_dir)
        self.client_dir = paths["client_dir"]
        self.downloads_dir = paths["downloads_dir"]
        self.pending_updates_dir = paths["pending_updates_dir"]
        self.data_dir = paths["data_dir"]
        self.state_path = paths["state_path"]

        # RLock makes the class safe even if a caller ends up invoking one
        # method from inside another lock-protected LocalState flow.
        self.lock = threading.RLock()

        self._ensure_storage()
        self.state = self._load_or_create_state()

    def _ensure_storage(self) -> None:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.pending_updates_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _load_or_create_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding=ENCODING))
                if isinstance(loaded, dict):
                    loaded.setdefault("client_id", self.client_id)

                    apps = loaded.get("apps")
                    if not isinstance(apps, dict):
                        loaded["apps"] = {}

                    return loaded
            except json.JSONDecodeError:
                pass

        state = {
            "client_id": self.client_id,
            "apps": {},
        }
        self._save_state_unlocked(state)
        return state

    def _save_state_unlocked(self, state: dict[str, Any]) -> None:
        tmp_path = self.state_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(state, indent=JSON_INDENT),
            encoding=ENCODING,
        )
        os.replace(tmp_path, self.state_path)

    def save_state(self) -> None:
        with self.lock:
            self._save_state_unlocked(self.state)

    def get_local_apps_payload(self) -> list[dict[str, Any]]:
        with self.lock:
            apps = self.state.get("apps", {})
            if not isinstance(apps, dict):
                return []

            payload: list[dict[str, Any]] = []
            for app_name, data in apps.items():
                if not isinstance(app_name, str) or not isinstance(data, dict):
                    continue

                version = data.get("version")
                hash_value = data.get("hash")

                if isinstance(version, int) and isinstance(hash_value, str):
                    payload.append(
                        {
                            "name": app_name,
                            "version": version,
                            "hash": hash_value,
                        }
                    )

            payload.sort(key=lambda item: item["name"])
            return payload

    def is_app_locked(self, app_name: str) -> bool:
        with self.lock:
            return self._lock_path(app_name).exists()

    def lock_app(self, app_name: str) -> None:
        with self.lock:
            self._lock_path(app_name).write_text("locked", encoding=ENCODING)

    def unlock_app(self, app_name: str) -> None:
        with self.lock:
            lock_path = self._lock_path(app_name)
            if lock_path.exists():
                lock_path.unlink()

    def write_installed_file(
        self,
        app_name: str,
        file_data: bytes,
        version: int,
        hash_value: str,
    ) -> None:
        with self.lock:
            tmp_path = self._download_tmp_path(app_name)
            final_path = self._download_path(app_name)

            tmp_path.write_bytes(file_data)
            os.replace(tmp_path, final_path)

            self.state.setdefault("apps", {})
            self.state["apps"][app_name] = {
                "version": version,
                "hash": hash_value,
            }
            self._save_state_unlocked(self.state)

    def write_pending_update(
        self,
        app_name: str,
        file_data: bytes,
        version: int,
        hash_value: str,
    ) -> None:
        with self.lock:
            pending_tmp = self._pending_tmp_path(app_name)
            pending_new = self._pending_new_path(app_name)

            meta_tmp = self._pending_meta_tmp_path(app_name)
            meta_final = self._pending_meta_path(app_name)

            pending_tmp.write_bytes(file_data)
            os.replace(pending_tmp, pending_new)

            meta_payload = {
                "version": version,
                "hash": hash_value,
            }
            meta_tmp.write_text(
                json.dumps(meta_payload, indent=JSON_INDENT),
                encoding=ENCODING,
            )
            os.replace(meta_tmp, meta_final)

    def _read_pending_metadata_unlocked(self, app_name: str) -> dict[str, Any]:
        data = json.loads(self._pending_meta_path(app_name).read_text(encoding=ENCODING))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid metadata format for {app_name}")
        return data

    def apply_pending_update(
        self,
        app_name: str,
        version: int | None = None,
        hash_value: str | None = None,
    ) -> bool:
        with self.lock:
            new_path = self._pending_new_path(app_name)
            meta_path = self._pending_meta_path(app_name)

            if not new_path.exists() or not meta_path.exists():
                return False

            if self.is_app_locked(app_name):
                return False

            resolved_version = version
            resolved_hash = hash_value

            if resolved_version is None or resolved_hash is None:
                metadata = self._read_pending_metadata_unlocked(app_name)
                meta_version = metadata.get("version")
                meta_hash = metadata.get("hash")

                if not isinstance(meta_version, int) or not isinstance(meta_hash, str):
                    raise ValueError(f"Invalid pending metadata values for {app_name}")

                resolved_version = meta_version
                resolved_hash = meta_hash

            os.replace(new_path, self._download_path(app_name))

            self.state.setdefault("apps", {})
            self.state["apps"][app_name] = {
                "version": resolved_version,
                "hash": resolved_hash,
            }
            self._save_state_unlocked(self.state)

            if meta_path.exists():
                meta_path.unlink()

            return True

    def delete_orphan_pending_binary(self, app_name: str) -> None:
        with self.lock:
            new_path = self._pending_new_path(app_name)
            if new_path.exists():
                new_path.unlink()

    def delete_orphan_pending_meta(self, app_name: str) -> None:
        with self.lock:
            meta_path = self._pending_meta_path(app_name)
            if meta_path.exists():
                meta_path.unlink()

    def list_pending_app_names(self) -> list[str]:
        with self.lock:
            app_names = [
                path.name.removesuffix(".new")
                for path in self.pending_updates_dir.glob("*.new")
            ]
            app_names.sort()
            return app_names

    def list_pending_meta_app_names(self) -> list[str]:
        with self.lock:
            app_names = [
                path.name.removesuffix(".meta")
                for path in self.pending_updates_dir.glob("*.meta")
            ]
            app_names.sort()
            return app_names

    def pending_meta_exists(self, app_name: str) -> bool:
        with self.lock:
            return self._pending_meta_path(app_name).exists()

    def pending_new_exists(self, app_name: str) -> bool:
        with self.lock:
            return self._pending_new_path(app_name).exists()

    def read_pending_metadata(self, app_name: str) -> dict[str, Any]:
        with self.lock:
            return self._read_pending_metadata_unlocked(app_name)

    def _download_path(self, app_name: str) -> Path:
        return self.downloads_dir / app_name

    def _download_tmp_path(self, app_name: str) -> Path:
        return self.downloads_dir / f"{app_name}.tmp"

    def _pending_new_path(self, app_name: str) -> Path:
        return self.pending_updates_dir / f"{app_name}.new"

    def _pending_tmp_path(self, app_name: str) -> Path:
        return self.pending_updates_dir / f"{app_name}.tmp"

    def _pending_meta_path(self, app_name: str) -> Path:
        return self.pending_updates_dir / f"{app_name}.meta"

    def _pending_meta_tmp_path(self, app_name: str) -> Path:
        return self.pending_updates_dir / f"{app_name}.meta.tmp"

    def _lock_path(self, app_name: str) -> Path:
        return self.downloads_dir / f"{app_name}.lock"