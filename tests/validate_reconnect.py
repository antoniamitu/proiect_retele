"""
validate_reconnect.py — Validare reconnect + CHECK_UPDATES pentru client

Ce demonstrează:
  1. Un client real descarcă o aplicație de pe server
  2. Clientul se oprește (offline)
  3. Pe server se publică o versiune nouă pentru aceeași aplicație
  4. Clientul pornește din nou cu aceeași instanță
  5. La reconnect, clientul face CHECK_UPDATES și recuperează versiunea nouă

Scriptul pornește singur serverul și clientul, pregătește datele demo și
verifică explicit actualizarea pe disk și în client_state.json.

Rulare:
    python tests/validate_reconnect.py
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Configurare
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import generate_demo_apps  # noqa: E402 — requires PROJECT_ROOT on sys.path

SERVER_APPS_DIR = PROJECT_ROOT / "server" / "apps"
SERVER_DATA_DIR = PROJECT_ROOT / "server" / "data"
APPS_MANIFEST_PATH = SERVER_DATA_DIR / "apps_manifest.json"
DOWNLOADS_REGISTRY_PATH = SERVER_DATA_DIR / "downloads_registry.json"

INSTANCES_DIR = PROJECT_ROOT / "client_instances"
CLIENT_INSTANCE = "client_reconnect"
CLIENT_ID = "client_reconnect"

CLIENT_INSTANCE_DIR = INSTANCES_DIR / CLIENT_INSTANCE
CLIENT_DOWNLOADS_DIR = CLIENT_INSTANCE_DIR / "downloads"
CLIENT_STATE_PATH = CLIENT_INSTANCE_DIR / "data" / "client_state.json"

HOST = "127.0.0.1"
PORT = 9000

APP_NAME = "notes.exe"
APP_V1_BYTES = generate_demo_apps.pad_bytes(b"NOTES_DEMO_V1", 5120)
APP_V2_BYTES = generate_demo_apps.pad_bytes(b"NOTES_DEMO_V2", 5120)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
INFO = "\033[94mINFO\033[0m"

_results: list[tuple[str, bool]] = []


# ---------------------------------------------------------------------------
# Utilitare output
# ---------------------------------------------------------------------------

def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    suffix = f"\n        → {detail}" if detail and not condition else ""
    print(f"  [{status}] {name}{suffix}")
    _results.append((name, condition))


def info(msg: str) -> None:
    print(f"  [{INFO}] {msg}")


def section(title: str) -> None:
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


# ---------------------------------------------------------------------------
# Helpers fișiere
# ---------------------------------------------------------------------------

def write_demo_apps() -> None:
    SERVER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    generate_demo_apps.write_demo_app_binaries(reset_manifest=False)


def reset_server_state() -> None:
    APPS_MANIFEST_PATH.write_text("{}", encoding="utf-8")
    DOWNLOADS_REGISTRY_PATH.write_text("{}", encoding="utf-8")


def cleanup_client_instance() -> None:
    if CLIENT_INSTANCE_DIR.exists():
        shutil.rmtree(CLIENT_INSTANCE_DIR)


def read_client_state() -> dict:
    if not CLIENT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(CLIENT_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_manifest() -> dict:
    if not APPS_MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(APPS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Runner generic pentru subprocess cu stdout reader thread
# ---------------------------------------------------------------------------

class ProcessRunner:
    def __init__(self, name: str, cmd: list[str], cwd: Path) -> None:
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.reader_thread: threading.Thread | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(self.cwd),
        )
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def _reader_loop(self) -> None:
        if not self.proc or not self.proc.stdout:
            return
        try:
            for line in self.proc.stdout:
                if not line:
                    break
                self.output_queue.put(line.rstrip("\n"))
        except Exception:
            pass

    def send(self, command: str) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError(f"Procesul {self.name} nu mai rulează.")
        assert self.proc.stdin is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def wait_for_output(self, markers: list[str], timeout: float = 15.0) -> str:
        collected: list[str] = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                break
            try:
                line = self.output_queue.get(timeout=0.2)
                collected.append(line)
                print(f"    [{self.name}] {line}")
                if any(marker in line for marker in markers):
                    break
            except queue.Empty:
                continue

        return "\n".join(collected)

    def collect_for(self, seconds: float = 1.5) -> str:
        collected: list[str] = []
        deadline = time.time() + seconds

        while time.time() < deadline:
            try:
                line = self.output_queue.get(timeout=0.2)
                collected.append(line)
                print(f"    [{self.name}] {line}")
            except queue.Empty:
                continue

        return "\n".join(collected)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, graceful_command: str | None = None) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                if graceful_command:
                    self.send(graceful_command)
                    self.proc.wait(timeout=5)
                else:
                    self.proc.terminate()
                    self.proc.wait(timeout=5)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()


# ---------------------------------------------------------------------------
# Runner server
# ---------------------------------------------------------------------------

class ServerRunner(ProcessRunner):
    def __init__(self) -> None:
        super().__init__(
            name="server",
            cmd=[sys.executable, "-m", "server.src.server_main"],
            cwd=PROJECT_ROOT,
        )

    def start_and_wait_ready(self) -> None:
        self.start()
        self.wait_for_output(
            ["Server listening on", "Available commands:"],
            timeout=20,
        )

    def publish(self, app_name: str) -> str:
        self.send(f"publish {app_name}")
        out = self.collect_for(2.0)
        return out


# ---------------------------------------------------------------------------
# Runner client
# ---------------------------------------------------------------------------

class ClientRunner(ProcessRunner):
    def __init__(self, instance_name: str, client_id: str) -> None:
        super().__init__(
            name=instance_name,
            cmd=[
                sys.executable,
                "-m",
                "client.src.main_client",
                "--client-id",
                client_id,
                "--client-instance",
                instance_name,
                "--host",
                HOST,
                "--port",
                str(PORT),
            ],
            cwd=PROJECT_ROOT,
        )

    def start_and_wait_ready(self) -> None:
        self.start()
        self.wait_for_output(
            [
                "Connected successfully",
                "No updates available.",
                "Updates available",
                "Download completed",
                "Available client commands:",
            ],
            timeout=20,
        )

    def download(self, app_name: str) -> str:
        self.send(f"download {app_name}")
        return self.wait_for_output(
            ["Download completed", "Download failed", "does not exist"],
            timeout=20,
        )

    def stop_client(self) -> None:
        self.stop(graceful_command="exit")


# ---------------------------------------------------------------------------
# Validare
# ---------------------------------------------------------------------------

def validate_reconnect_flow() -> None:
    section("PREGĂTIRE")
    info("Curăț client instance vechi...")
    cleanup_client_instance()

    info("Resetez manifestul și registry-ul serverului...")
    reset_server_state()

    info("Scriu aplicațiile demo...")
    write_demo_apps()

    server = ServerRunner()
    client = ClientRunner(CLIENT_INSTANCE, CLIENT_ID)

    try:
        section("PORNIRE SERVER")
        server.start_and_wait_ready()
        check("Server pornit", server.is_alive())

        section("PORNIRE CLIENT + DOWNLOAD V1")
        client.start_and_wait_ready()
        check("Client pornit", client.is_alive())

        out = client.download(APP_NAME)
        check("Download inițial reușește", "Download completed" in out, out)

        notes_path = CLIENT_DOWNLOADS_DIR / APP_NAME
        check("Fișierul notes.exe există local după download", notes_path.exists())

        state = read_client_state()
        app_entry = state.get("apps", {}).get(APP_NAME, {})
        check("client_state.json conține notes.exe după download", APP_NAME in state.get("apps", {}))
        check("Versiunea locală inițială este 1", app_entry.get("version") == 1)

        if notes_path.exists():
            check("Conținutul local este NOTES_V1", notes_path.read_bytes() == APP_V1_BYTES)

        section("OPRIRE CLIENT (offline)")
        client.stop_client()
        check("Client oprit", not client.is_alive())

        section("PUBLICARE V2 PE SERVER")
        info("Suprascriu notes.exe cu versiunea 2...")
        (SERVER_APPS_DIR / APP_NAME).write_bytes(APP_V2_BYTES)

        info("Rulez publish notes.exe pe server...")
        publish_out = server.publish(APP_NAME)

        manifest = read_manifest()
        app_manifest = manifest.get(APP_NAME, {})
        check("Manifestul serverului conține notes.exe", APP_NAME in manifest)
        check("Versiunea de pe server a devenit 2", app_manifest.get("version") == 2, json.dumps(app_manifest, ensure_ascii=False))
        published_hash = app_manifest.get("hash")
        check("Hash-ul publicat pe server există", isinstance(published_hash, str) and len(published_hash) == 64, publish_out)

        section("RECONNECT CLIENT + CHECK_UPDATES")
        client = ClientRunner(CLIENT_INSTANCE, CLIENT_ID)
        client.start_and_wait_ready()

        # Colectează încă puțin output pentru CHECK_UPDATES automat
        reconnect_out = client.collect_for(3.0)

        check("Client repornit", client.is_alive())
        check(
            "La reconnect apare activitate de update",
            (
                "Updates available" in reconnect_out
                or "Download completed" in reconnect_out
                or "Installed notes.exe v2" in reconnect_out
                or "Installed notes.exe v" in reconnect_out
            ),
            reconnect_out,
        )

        state = read_client_state()
        app_entry = state.get("apps", {}).get(APP_NAME, {})
        check("client_state.json conține în continuare notes.exe", APP_NAME in state.get("apps", {}))
        check("Versiunea locală după reconnect este 2", app_entry.get("version") == 2, json.dumps(app_entry, ensure_ascii=False))
        check("Hash-ul local după reconnect coincide cu manifestul serverului", app_entry.get("hash") == published_hash)

        check("Fișierul notes.exe există după reconnect", notes_path.exists())
        if notes_path.exists():
            check("Conținutul local este NOTES_V2 după reconnect", notes_path.read_bytes() == APP_V2_BYTES)

    finally:
        section("OPRIRE")
        info("Opresc clientul...")
        try:
            client.stop_client()
        except Exception:
            pass
        check("Client oprit la final", not client.is_alive() if 'client' in locals() else True)

        info("Opresc serverul...")
        try:
            server.stop(graceful_command="exit")
        except Exception:
            pass
        check("Server oprit la final", not server.is_alive() if 'server' in locals() else True)


# ---------------------------------------------------------------------------
# Sumar
# ---------------------------------------------------------------------------

def print_summary() -> None:
    print(f"\n{'=' * 55}")
    print("SUMAR VALIDARE RECONNECT")
    print(f"{'=' * 55}")

    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    total = len(_results)

    if failed:
        print(f"\nValidări eșuate ({failed}):")
        for name, ok in _results:
            if not ok:
                print(f"  {FAIL}  {name}")

    print(f"\nRezultat final: {passed}/{total} validări trecute")

    if failed == 0:
        print(f"\n{PASS} Toate validările au trecut! Reconnect + CHECK_UPDATES funcționează corect.")
    else:
        print(f"\n{FAIL} {failed} validări au eșuat.")

    print(f"{'=' * 55}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("VALIDARE RECONNECT CLIENT")
    print("=" * 55)

    validate_reconnect_flow()
    print_summary()

    failed_count = sum(1 for _, ok in _results if not ok)
    sys.exit(0 if failed_count == 0 else 1)