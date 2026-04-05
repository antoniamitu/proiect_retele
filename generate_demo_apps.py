from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
apps_dir = PROJECT_ROOT / "server" / "apps"
data_dir = PROJECT_ROOT / "server" / "data"

apps_dir.mkdir(parents=True, exist_ok=True)
data_dir.mkdir(parents=True, exist_ok=True)

apps = {
    "calculator.exe": b"CALCULATOR_DEMO_V1" * 64,
    "notes.exe": b"NOTES_DEMO_V1" * 256,
    "game.exe": b"GAME_DEMO_V1" * 1024,
}

for name, content in apps.items():
    path = apps_dir / name
    path.write_bytes(content)
    print(f"Generat {name} ({len(content)} bytes) -> {path}")

# reset manifest + registry ca serverul sa recalculeze corect hash-urile
(apps_dir.parent / "data" / "apps_manifest.json").write_text("{}", encoding="utf-8")
(apps_dir.parent / "data" / "downloads_registry.json").write_text("{}", encoding="utf-8")

print("\nManifestul si registry-ul au fost resetate.")
print("Repornește serverul pentru a reîncărca aplicațiile și hash-urile.")