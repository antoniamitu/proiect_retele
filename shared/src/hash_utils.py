from __future__ import annotations

import hashlib
from pathlib import Path

HASH_CHUNK_SIZE = 8192


def compute_hash(file_data: bytes) -> str:
    return hashlib.sha256(file_data).hexdigest()


def compute_file_hash(file_path: str | Path) -> str:
    path = Path(file_path)
    hasher = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)

    return hasher.hexdigest()