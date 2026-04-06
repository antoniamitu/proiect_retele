"""
Generate deterministic demo binaries under server/apps/ and reset server JSON state.

Sizes match README Section 24: calculator 1 KiB, notes 5 KiB, game 20 KiB.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
APPS_DIR = PROJECT_ROOT / "server" / "apps"
DATA_DIR = PROJECT_ROOT / "server" / "data"

# (label prefix, exact byte length) — same sizes as README Section 24
DEMO_APPS: dict[str, tuple[bytes, int]] = {
    "calculator.exe": (b"CALCULATOR_DEMO_V1", 1024),
    "notes.exe": (b"NOTES_DEMO_V1", 5120),
    "game.exe": (b"GAME_DEMO_V1", 20480),
}


def pad_bytes(prefix: bytes, size: int) -> bytes:
    """Repeat prefix to fill exactly `size` bytes (deterministic, reproducible)."""
    if not prefix:
        raise ValueError("prefix must be non-empty")
    repeat = (size + len(prefix) - 1) // len(prefix)
    return (prefix * repeat)[:size]


def write_demo_app_binaries(*, reset_manifest: bool = True) -> None:
    """Write demo .exe payloads. Optionally reset apps_manifest.json and downloads_registry.json."""
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for name, (prefix, size) in DEMO_APPS.items():
        path = APPS_DIR / name
        path.write_bytes(pad_bytes(prefix, size))
        print(f"Generated {name} ({size} bytes) -> {path}")

    if reset_manifest:
        (DATA_DIR / "apps_manifest.json").write_text("{}", encoding="utf-8")
        (DATA_DIR / "downloads_registry.json").write_text("{}", encoding="utf-8")
        print("\nReset server/data/apps_manifest.json and server/data/downloads_registry.json.")
        print("Restart the server so it reloads apps and recomputes hashes (bootstrap).")


if __name__ == "__main__":
    write_demo_app_binaries(reset_manifest=True)
