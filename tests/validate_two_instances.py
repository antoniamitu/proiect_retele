from __future__ import annotations

import argparse
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
INSTANCES_DIR = PROJECT_ROOT / "client_instances"

INSTANCE_A = "client_mara_a"
INSTANCE_B = "client_mara_b"

CLIENT_ID_A = "client_mara_a"
CLIENT_ID_B = "client_mara_b"

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
INFO = "\033[94mINFO\033[0m"

_results: list[tuple[str, bool]] = []


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
# Helpers directoare / stare
# ---------------------------------------------------------------------------

def instance_dir(instance_name: str) -> Path:
    return INSTANCES_DIR / instance_name


def downloads_dir(instance_name: str) -> Path:
    return instance_dir(instance_name) / "downloads"


def pending_dir(instance_name: str) -> Path:
    return instance_dir(instance_name) / "pending_updates"


def state_path(instance_name: str) -> Path:
    return instance_dir(instance_name) / "data" / "client_state.json"


def read_state(instance_name: str) -> dict:
    p = state_path(instance_name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def list_downloads(instance_name: str) -> list[str]:
    d = downloads_dir(instance_name)
    if not d.exists():
        return []
    return sorted(
        f.name for f in d.iterdir()
        if f.is_file() and not f.name.endswith((".lock", ".tmp"))
    )


def list_pending(instance_name: str) -> list[str]:
    d = pending_dir(instance_name)
    if not d.exists():
        return []
    return sorted(f.name for f in d.iterdir() if f.is_file())


def is_locked(instance_name: str, app_name: str) -> bool:
    return (downloads_dir(instance_name) / f"{app_name}.lock").exists()


def cleanup_instances() -> None:
    for name in [INSTANCE_A, INSTANCE_B]:
        d = instance_dir(name)
        if d.exists():
            shutil.rmtree(d)
            info(f"Șters director anterior: {d.relative_to(PROJECT_ROOT)}")


# ---------------------------------------------------------------------------
# Runner de subproces pentru client
# ---------------------------------------------------------------------------

class ClientRunner:
    """
    Pornește un proces client real ca subprocess și îi trimite comenzi prin stdin.
    Colectează output-ul din stdout printr-un thread dedicat, compatibil cu Windows.
    """

    def __init__(
        self,
        instance_name: str,
        client_id: str,
        host: str,
        port: int,
    ) -> None:
        self.instance_name = instance_name
        self.client_id = client_id
        self.host = host
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.reader_thread: threading.Thread | None = None

    def start(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "client.src.main_client",
            "--client-id",
            self.client_id,
            "--client-instance",
            self.instance_name,
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(PROJECT_ROOT),
        )

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        self._wait_for_output(
            [
                "Connected successfully",
                "No updates",
                "Updates available",
                "Download completed",
                "Available client commands:",
                "client_mara_a>",
                "client_mara_b>",
            ],
            timeout=20,
        )

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

    def send(
        self,
        command: str,
        wait_for: list[str] | None = None,
        timeout: float = 15.0,
    ) -> str:
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError(f"Procesul {self.instance_name} nu mai rulează.")

        assert self.proc.stdin is not None
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

        if wait_for:
            return self._wait_for_output(wait_for, timeout=timeout)

        time.sleep(1.0)
        return self._drain_output()

    def _drain_output(self) -> str:
        collected: list[str] = []
        while True:
            try:
                line = self.output_queue.get_nowait()
                collected.append(line)
                print(f"    [{self.instance_name}] {line}")
            except queue.Empty:
                break
        return "\n".join(collected)

    def _wait_for_output(self, markers: list[str], timeout: float = 15.0) -> str:
        collected: list[str] = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                break

            try:
                line = self.output_queue.get(timeout=0.2)
                collected.append(line)
                print(f"    [{self.instance_name}] {line}")
                if any(marker in line for marker in markers):
                    break
            except queue.Empty:
                continue

        return "\n".join(collected)

    def collect_for(self, seconds: float = 1.5) -> str:
        """
        Colectează tot output-ul disponibil pentru un interval scurt.
        Util pentru comenzi cu output multi-linie.
        """
        collected: list[str] = []
        deadline = time.time() + seconds

        while time.time() < deadline:
            try:
                line = self.output_queue.get(timeout=0.2)
                collected.append(line)
                print(f"    [{self.instance_name}] {line}")
            except queue.Empty:
                continue

        return "\n".join(collected)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write("exit\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# ---------------------------------------------------------------------------
# Validări
# ---------------------------------------------------------------------------

def validate_directory_isolation() -> None:
    section("VALIDARE 1 — Directoare izolate create corect")

    expected = [
        (INSTANCE_A, CLIENT_ID_A),
        (INSTANCE_B, CLIENT_ID_B),
    ]

    for instance_name, client_id in expected:
        check(
            f"{instance_name}: director instanță creat",
            instance_dir(instance_name).exists(),
            f"Lipsă: {instance_dir(instance_name)}",
        )
        check(f"{instance_name}: downloads/ există", downloads_dir(instance_name).exists())
        check(f"{instance_name}: pending_updates/ există", pending_dir(instance_name).exists())
        check(f"{instance_name}: data/client_state.json există", state_path(instance_name).exists())

        state = read_state(instance_name)
        check(
            f"{instance_name}: client_id corect în client_state.json",
            state.get("client_id") == client_id,
            f"Găsit: {state.get('client_id')}",
        )

    check(
        "Directoarele celor două instanțe sunt diferite",
        instance_dir(INSTANCE_A) != instance_dir(INSTANCE_B),
    )


def validate_list_apps(client_a: ClientRunner, client_b: ClientRunner) -> None:
    section("VALIDARE 2 — Ambele instanțe văd aceeași listă de aplicații")

    info("Instanța A rulează 'list'...")
    client_a.send("list", wait_for=["Available apps", "No apps"], timeout=10)
    out_a = client_a.collect_for(1.5)

    info("Instanța B rulează 'list'...")
    client_b.send("list", wait_for=["Available apps", "No apps"], timeout=10)
    out_b = client_b.collect_for(1.5)

    a_sees_apps = all(app in out_a for app in ["calculator.exe", "notes.exe", "game.exe"])
    b_sees_apps = all(app in out_b for app in ["calculator.exe", "notes.exe", "game.exe"])

    check("Instanța A vede toate aplicațiile de pe server", a_sees_apps, out_a)
    check("Instanța B vede toate aplicațiile de pe server", b_sees_apps, out_b)


def validate_download_isolation(client_a: ClientRunner, client_b: ClientRunner) -> None:
    section("VALIDARE 3 — Download izolat: instanța A descarcă calculator.exe")

    info("Instanța A descarcă calculator.exe...")
    out = client_a.send(
        "download calculator.exe",
        wait_for=["Download completed", "Download failed", "does not exist"],
        timeout=20,
    )
    time.sleep(1)

    downloads_a = list_downloads(INSTANCE_A)
    downloads_b = list_downloads(INSTANCE_B)

    info(f"Instanța A downloads/: {downloads_a}")
    info(f"Instanța B downloads/: {downloads_b}")

    check(
        "Download calculator.exe reușește la instanța A",
        "Download completed" in out,
        out,
    )
    check("calculator.exe există în downloads/ al instanței A", "calculator.exe" in downloads_a)
    check("calculator.exe NU există în downloads/ al instanței B", "calculator.exe" not in downloads_b)

    state_a = read_state(INSTANCE_A)
    state_b = read_state(INSTANCE_B)

    check("client_state.json A înregistrează calculator.exe", "calculator.exe" in state_a.get("apps", {}))
    check("client_state.json B NU înregistrează calculator.exe", "calculator.exe" not in state_b.get("apps", {}))


def validate_independent_downloads(client_a: ClientRunner, client_b: ClientRunner) -> None:
    section("VALIDARE 4 — Instanța B descarcă notes.exe independent")

    info("Instanța B descarcă notes.exe...")
    out = client_b.send(
        "download notes.exe",
        wait_for=["Download completed", "Download failed", "does not exist"],
        timeout=20,
    )
    time.sleep(1)

    downloads_a = list_downloads(INSTANCE_A)
    downloads_b = list_downloads(INSTANCE_B)

    info(f"Instanța A downloads/: {downloads_a}")
    info(f"Instanța B downloads/: {downloads_b}")

    check(
        "Download notes.exe reușește la instanța B",
        "Download completed" in out,
        out,
    )
    check("notes.exe există în downloads/ al instanței B", "notes.exe" in downloads_b)
    check("notes.exe NU există în downloads/ al instanței A", "notes.exe" not in downloads_a)

    state_a = read_state(INSTANCE_A)
    state_b = read_state(INSTANCE_B)

    check("client_state.json B înregistrează notes.exe", "notes.exe" in state_b.get("apps", {}))
    check("client_state.json A NU înregistrează notes.exe", "notes.exe" not in state_a.get("apps", {}))

    check("Instanța A are doar calculator.exe", "calculator.exe" in downloads_a and "notes.exe" not in downloads_a)
    check("Instanța B are doar notes.exe", "notes.exe" in downloads_b and "calculator.exe" not in downloads_b)


def validate_lock_isolation(client_a: ClientRunner, client_b: ClientRunner) -> None:
    section("VALIDARE 5 — Lock pe instanța A NU afectează instanța B")

    info("Instanța A blochează calculator.exe...")
    client_a.send("lock calculator.exe", timeout=3)
    time.sleep(0.5)

    locked_a = is_locked(INSTANCE_A, "calculator.exe")
    locked_b = is_locked(INSTANCE_B, "calculator.exe")

    check("calculator.exe.lock există în downloads/ A", locked_a)
    check("calculator.exe.lock NU există în downloads/ B", not locked_b)

    info("Instanța A deblochează calculator.exe...")
    client_a.send("unlock calculator.exe", timeout=3)
    time.sleep(0.5)

    check("Lock eliminat corect la A", not is_locked(INSTANCE_A, "calculator.exe"))
    check("Instanța B rămâne neafectată", not is_locked(INSTANCE_B, "calculator.exe"))


def validate_state_command(client_a: ClientRunner, client_b: ClientRunner) -> None:
    section("VALIDARE 6 — Comanda 'state' afișează state-ul corect per instanță")

    info("Instanța A rulează 'state'...")
    client_a.send("state", wait_for=["Installed state"], timeout=5)
    out_a = client_a.collect_for(1.0)

    info("Instanța B rulează 'state'...")
    client_b.send("state", wait_for=["Installed state"], timeout=5)
    out_b = client_b.collect_for(1.0)

    check("Output 'state' A conține client_id-ul corect", CLIENT_ID_A in out_a, out_a)
    check("Output 'state' B conține client_id-ul corect", CLIENT_ID_B in out_b, out_b)


def validate_pending_isolation(client_a: ClientRunner, client_b: ClientRunner) -> None:
    section("VALIDARE 7 — Pending updates izolate per instanță")

    if not client_a.is_alive():
        check("Instanța A este încă activă înainte de testul pending", False)
        return

    if not client_b.is_alive():
        check("Instanța B este încă activă înainte de testul pending", False)
        return

    info("Instanța A descarcă game.exe...")
    out = client_a.send(
        "download game.exe",
        wait_for=["Download completed", "Download failed", "does not exist"],
        timeout=20,
    )
    time.sleep(0.5)

    if "Download completed" not in out:
        check("Download game.exe reușește înainte de testul pending", False, out)
        return

    info("Instanța A blochează game.exe...")
    client_a.send("lock game.exe", timeout=3)
    time.sleep(0.5)

    check("game.exe instalat la A", "game.exe" in list_downloads(INSTANCE_A))
    check("game.exe blocat la A", is_locked(INSTANCE_A, "game.exe"))
    check("game.exe NU există la B", "game.exe" not in list_downloads(INSTANCE_B))

    pending_a = list_pending(INSTANCE_A)
    pending_b = list_pending(INSTANCE_B)

    info(f"Pending A: {pending_a}")
    info(f"Pending B: {pending_b}")

    check(
        "pending_updates/ al instanței B nu conține fișiere ale instanței A",
        not any("game.exe" in f for f in pending_b),
    )


# ---------------------------------------------------------------------------
# Sumar
# ---------------------------------------------------------------------------

def print_summary() -> None:
    print(f"\n{'=' * 55}")
    print("SUMAR VALIDARE DOUĂ INSTANȚE")
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
        print(f"\n{PASS} Toate validările au trecut! Izolarea instanțelor funcționează corect.")
    else:
        print(f"\n{FAIL} {failed} validări au eșuat.")

    print(f"{'=' * 55}")

    print("\nStructură directoare create:")
    for name in [INSTANCE_A, INSTANCE_B]:
        d = instance_dir(name)
        if d.exists():
            print(f"\n  {d.relative_to(PROJECT_ROOT)}/")
            for sub in ["downloads", "pending_updates", "data"]:
                sub_dir = d / sub
                if sub_dir.exists():
                    files = [f.name for f in sub_dir.iterdir() if f.is_file()]
                    files_str = ", ".join(files) if files else "(gol)"
                    print(f"    {sub}/ → {files_str}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validare două instanțe reale de client simultan")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=9000, help="Server port")
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Nu șterge directoarele instanțelor anterioare înainte de test",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("=" * 55)
    print("VALIDARE DOUĂ INSTANȚE CLIENT SIMULTANE")
    print(f"Server: {args.host}:{args.port}")
    print("=" * 55)

    if not args.no_cleanup:
        print("\nCurățare instanțe anterioare...")
        cleanup_instances()

    client_a = ClientRunner(INSTANCE_A, CLIENT_ID_A, args.host, args.port)
    client_b = ClientRunner(INSTANCE_B, CLIENT_ID_B, args.host, args.port)

    try:
        section("PORNIRE INSTANȚE")

        info(f"Pornesc {INSTANCE_A}...")
        try:
            client_a.start()
            check(f"{INSTANCE_A} pornit și conectat", client_a.is_alive())
        except Exception as exc:
            check(f"{INSTANCE_A} pornit și conectat", False, str(exc))
            print(f"\n{FAIL} Nu s-a putut porni prima instanță. Verifică dacă serverul rulează.")
            return

        info(f"Pornesc {INSTANCE_B}...")
        try:
            client_b.start()
            check(f"{INSTANCE_B} pornit și conectat", client_b.is_alive())
        except Exception as exc:
            check(f"{INSTANCE_B} pornit și conectat", False, str(exc))
            print(f"\n{FAIL} Nu s-a putut porni a doua instanță.")
            return

        time.sleep(1)

        validate_directory_isolation()
        validate_list_apps(client_a, client_b)
        validate_download_isolation(client_a, client_b)
        validate_independent_downloads(client_a, client_b)
        validate_lock_isolation(client_a, client_b)
        validate_state_command(client_a, client_b)
        validate_pending_isolation(client_a, client_b)

    finally:
        section("OPRIRE INSTANȚE")
        info(f"Opresc {INSTANCE_A}...")
        client_a.stop()
        info(f"Opresc {INSTANCE_B}...")
        client_b.stop()

        check(f"{INSTANCE_A} oprit", not client_a.is_alive())
        check(f"{INSTANCE_B} oprit", not client_b.is_alive())

        print_summary()

    failed_count = sum(1 for _, ok in _results if not ok)
    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    main()