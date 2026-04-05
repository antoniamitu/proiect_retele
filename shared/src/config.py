from __future__ import annotations

import os
from pathlib import Path

HOST = "0.0.0.0"  # server listens on all interfaces (needed for Docker)
PORT = 9000
BUFFER_SIZE = 4096
SOCKET_TIMEOUT = 5       # seconds — used for select() polling interval
FRAME_TIMEOUT = 10      # seconds — max time to read a complete frame once select() triggers
ACK_TIMEOUT = 10         # seconds — max wait for ACK after file transfer
RETRY_INTERVAL = 5       # seconds — client retry worker interval

# Client-side defaults
SERVER_HOST = "127.0.0.1"
SERVER_PORT = PORT

ENCODING = "utf-8"
JSON_INDENT = 2

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parents[2]

SERVER_DIR = PROJECT_ROOT / "server"
SERVER_APPS_DIR = SERVER_DIR / "apps"
SERVER_DATA_DIR = SERVER_DIR / "data"
APPS_MANIFEST_PATH = SERVER_DATA_DIR / "apps_manifest.json"
DOWNLOADS_REGISTRY_PATH = SERVER_DATA_DIR / "downloads_registry.json"

# Optional per-client instance selection for demos where multiple client
# processes run from the same project workspace.
#
# Examples:
#   CLIENT_INSTANCE=client_mara
#   CLIENT_INSTANCE=client_antonia
#   CLIENT_INSTANCE=client_auxeniu
CLIENT_INSTANCE = os.getenv("CLIENT_INSTANCE", "").strip()


def resolve_client_dir(
    client_instance: str | None = None,
    *,
    project_root: Path | None = None,
) -> Path:
    root = project_root if project_root is not None else PROJECT_ROOT
    instance = (client_instance if client_instance is not None else CLIENT_INSTANCE).strip()

    if instance:
        return root / "client_instances" / instance

    # Backward-compatible fallback
    return root / "client"


def build_client_paths(base_dir: Path) -> dict[str, Path]:
    client_dir = Path(base_dir)
    downloads_dir = client_dir / "downloads"
    pending_updates_dir = client_dir / "pending_updates"
    data_dir = client_dir / "data"

    return {
        "client_dir": client_dir,
        "downloads_dir": downloads_dir,
        "pending_updates_dir": pending_updates_dir,
        "data_dir": data_dir,
        "state_path": data_dir / "client_state.json",
    }


CLIENT_DIR = resolve_client_dir()
_CLIENT_PATHS = build_client_paths(CLIENT_DIR)

CLIENT_DOWNLOADS_DIR = _CLIENT_PATHS["downloads_dir"]
CLIENT_PENDING_UPDATES_DIR = _CLIENT_PATHS["pending_updates_dir"]
CLIENT_DATA_DIR = _CLIENT_PATHS["data_dir"]
CLIENT_STATE_PATH = _CLIENT_PATHS["state_path"]