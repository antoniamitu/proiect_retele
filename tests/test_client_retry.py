from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Rezolvă importurile: adaugă rădăcina proiectului în sys.path
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client.src.local_state import LocalState
from client.src.retry_worker import process_pending_updates, start_retry_worker
from client.src.update_manager import process_received_file

# ---------------------------------------------------------------------------
# Utilitare
# ---------------------------------------------------------------------------

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

_results: list[tuple[str, bool]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  [{status}] {name}{suffix}")
    _results.append((name, condition))


def make_local_state(tmp_dir: Path, client_id: str = "client_mara") -> LocalState:
    """Creează un LocalState cu directoare temporare izolate."""
    return LocalState(client_id, base_dir=tmp_dir)


def random_bytes(size: int = 1024) -> bytes:
    return os.urandom(size)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Test 1 — Install direct fără lock
# ---------------------------------------------------------------------------

def test_direct_install() -> None:
    print("\n[TEST 1] Install direct (fără lock)")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        file_data = random_bytes(1024)
        h = sha256(file_data)

        header = {
            "action": "FILE_TRANSFER",
            "app_name": "calculator.exe",
            "version": 1,
            "hash": h,
            "request_id": 1,
        }

        result = process_received_file(ls, header, file_data, log_func=print)
        check("process_received_file returnează True", result)

        installed = base / "downloads" / "calculator.exe"
        check("Fișier există în downloads/", installed.exists())
        check("Conținut corect pe disk", installed.read_bytes() == file_data)

        state = json.loads((base / "data" / "client_state.json").read_text(encoding="utf-8"))
        app_entry = state.get("apps", {}).get("calculator.exe", {})
        check("client_state.json actualizat cu versiunea corectă", app_entry.get("version") == 1)
        check("client_state.json actualizat cu hash-ul corect", app_entry.get("hash") == h)

        pending_new = base / "pending_updates" / "calculator.exe.new"
        pending_meta = base / "pending_updates" / "calculator.exe.meta"
        check("NU există .new în pending_updates/", not pending_new.exists())
        check("NU există .meta în pending_updates/", not pending_meta.exists())


# ---------------------------------------------------------------------------
# Test 2 — Update primit cât aplicația e locked → pending_updates/
# ---------------------------------------------------------------------------

def test_pending_when_locked() -> None:
    print("\n[TEST 2] Update cu lock activ → pending_updates/")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        # Instalează versiunea 1
        v1_data = random_bytes(1024)
        v1_hash = sha256(v1_data)
        ls.write_installed_file("notes.exe", v1_data, 1, v1_hash)

        # Blochează aplicația
        ls.lock_app("notes.exe")
        check("Lock creat corect", ls.is_app_locked("notes.exe"))

        # Primește versiunea 2
        v2_data = random_bytes(1024)
        v2_hash = sha256(v2_data)

        header = {
            "action": "FILE_TRANSFER",
            "app_name": "notes.exe",
            "version": 2,
            "hash": v2_hash,
            "request_id": 2,
        }

        result = process_received_file(ls, header, v2_data, log_func=print)
        check("process_received_file returnează True", result)

        pending_new = base / "pending_updates" / "notes.exe.new"
        pending_meta = base / "pending_updates" / "notes.exe.meta"
        check(".new există în pending_updates/", pending_new.exists())
        check(".meta există în pending_updates/", pending_meta.exists())

        # client_state.json trebuie să rămână la v1 (instalat)
        state = json.loads((base / "data" / "client_state.json").read_text(encoding="utf-8"))
        app_entry = state.get("apps", {}).get("notes.exe", {})
        check("client_state.json rămâne la v1 (instalat)", app_entry.get("version") == 1)

        # fișierul instalat trebuie să fie tot v1
        installed = base / "downloads" / "notes.exe"
        check("downloads/notes.exe rămâne la v1", installed.read_bytes() == v1_data)

        # meta trebuie să conțină v2
        meta = json.loads(pending_meta.read_text(encoding="utf-8"))
        check(".meta conține version=2", meta.get("version") == 2)
        check(".meta conține hash corect", meta.get("hash") == v2_hash)


# ---------------------------------------------------------------------------
# Test 3 — Retry worker aplică update după unlock
# ---------------------------------------------------------------------------

def test_retry_applies_after_unlock() -> None:
    print("\n[TEST 3] Retry worker aplică update după unlock")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        # Instalează v1 și blochează
        v1_data = random_bytes(1024)
        v1_hash = sha256(v1_data)
        ls.write_installed_file("game.exe", v1_data, 1, v1_hash)
        ls.lock_app("game.exe")

        # Pune v2 în pending
        v2_data = random_bytes(1024)
        v2_hash = sha256(v2_data)
        ls.write_pending_update("game.exe", v2_data, 2, v2_hash)

        logs: list[str] = []

        # Prima rulare: e blocat, nu aplică
        process_pending_updates(ls, log_func=logs.append)
        check("Retry nu aplică atunci când aplicația e blocată", ls.is_app_locked("game.exe"))
        locked_log = any("still locked" in msg for msg in logs)
        check("Retry loghează că aplicația e blocată", locked_log)

        # Deblochează
        ls.unlock_app("game.exe")
        check("Lock eliminat corect", not ls.is_app_locked("game.exe"))

        logs.clear()

        # A doua rulare: aplică
        process_pending_updates(ls, log_func=logs.append)

        installed = base / "downloads" / "game.exe"
        check("downloads/game.exe actualizat la v2", installed.read_bytes() == v2_data)

        state = json.loads((base / "data" / "client_state.json").read_text(encoding="utf-8"))
        app_entry = state.get("apps", {}).get("game.exe", {})
        check("client_state.json actualizat la v2", app_entry.get("version") == 2)
        check("client_state.json are hash-ul corect", app_entry.get("hash") == v2_hash)

        applied_log = any("Applied update" in msg for msg in logs)
        check("Retry loghează aplicarea update-ului", applied_log)

        pending_new = base / "pending_updates" / "game.exe.new"
        pending_meta = base / "pending_updates" / "game.exe.meta"
        check(".new șters după aplicare", not pending_new.exists())
        check(".meta șters după aplicare", not pending_meta.exists())


# ---------------------------------------------------------------------------
# Test 4 — Orphan .meta fără .new → curățat
# ---------------------------------------------------------------------------

def test_orphan_meta_cleanup() -> None:
    print("\n[TEST 4] Orphan .meta fără .new → curățat de retry worker")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        meta_path = base / "pending_updates" / "calculator.exe.meta"
        meta_path.write_text(
            json.dumps({"version": 2, "hash": "abc123"}),
            encoding="utf-8",
        )

        check(".meta există înainte de retry", meta_path.exists())
        check(".new NU există înainte de retry", not (base / "pending_updates" / "calculator.exe.new").exists())

        logs: list[str] = []
        process_pending_updates(ls, log_func=logs.append)

        check(".meta șters de retry worker", not meta_path.exists())
        orphan_log = any("orphaned metadata" in msg for msg in logs)
        check("Retry loghează ștergerea orphan .meta", orphan_log)


# ---------------------------------------------------------------------------
# Test 5 — Orphan .new fără .meta → curățat
# ---------------------------------------------------------------------------

def test_orphan_new_cleanup() -> None:
    print("\n[TEST 5] Orphan .new fără .meta → curățat de retry worker")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        new_path = base / "pending_updates" / "notes.exe.new"
        new_path.write_bytes(random_bytes(512))

        check(".new există înainte de retry", new_path.exists())
        check(".meta NU există înainte de retry", not (base / "pending_updates" / "notes.exe.meta").exists())

        logs: list[str] = []
        process_pending_updates(ls, log_func=logs.append)

        check(".new șters de retry worker", not new_path.exists())
        orphan_log = any("orphaned binary" in msg for msg in logs)
        check("Retry loghează ștergerea orphan .new", orphan_log)


# ---------------------------------------------------------------------------
# Test 6 — Izolare completă între două instanțe client
# ---------------------------------------------------------------------------

def test_instance_isolation() -> None:
    print("\n[TEST 6] Izolare completă între două instanțe (--client-instance)")

    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        base_a = Path(tmp_a)
        base_b = Path(tmp_b)

        ls_a = make_local_state(base_a, client_id="client_mara_a")
        ls_b = make_local_state(base_b, client_id="client_mara_b")

        # Instanța A descarcă calculator.exe
        data_a = random_bytes(1024)
        hash_a = sha256(data_a)
        ls_a.write_installed_file("calculator.exe", data_a, 1, hash_a)

        # Instanța B descarcă notes.exe
        data_b = random_bytes(2048)
        hash_b = sha256(data_b)
        ls_b.write_installed_file("notes.exe", data_b, 1, hash_b)

        a_calc = base_a / "downloads" / "calculator.exe"
        b_calc = base_b / "downloads" / "calculator.exe"
        b_notes = base_b / "downloads" / "notes.exe"
        a_notes = base_a / "downloads" / "notes.exe"

        check("Instanța A are calculator.exe", a_calc.exists())
        check("Instanța B NU are calculator.exe", not b_calc.exists())
        check("Instanța B are notes.exe", b_notes.exists())
        check("Instanța A NU are notes.exe", not a_notes.exists())

        state_a = json.loads((base_a / "data" / "client_state.json").read_text(encoding="utf-8"))
        state_b = json.loads((base_b / "data" / "client_state.json").read_text(encoding="utf-8"))

        check("client_state.json A are calculator.exe", "calculator.exe" in state_a.get("apps", {}))
        check("client_state.json A NU are notes.exe", "notes.exe" not in state_a.get("apps", {}))
        check("client_state.json B are notes.exe", "notes.exe" in state_b.get("apps", {}))
        check("client_state.json B NU are calculator.exe", "calculator.exe" not in state_b.get("apps", {}))

        ls_a.lock_app("calculator.exe")
        check("Lock din A nu afectează B", not ls_b.is_app_locked("calculator.exe"))

        check("client_id A corect", state_a.get("client_id") == "client_mara_a")
        check("client_id B corect", state_b.get("client_id") == "client_mara_b")


# ---------------------------------------------------------------------------
# Test 7 — Update mai nou suprascrie pending mai vechi
# ---------------------------------------------------------------------------

def test_newer_update_overwrites_pending() -> None:
    print("\n[TEST 7] Update mai nou suprascrie pending mai vechi")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        # Instalează v1 și blochează
        v1_data = random_bytes(512)
        v1_hash = sha256(v1_data)
        ls.write_installed_file("calculator.exe", v1_data, 1, v1_hash)
        ls.lock_app("calculator.exe")

        # Primește v2 → pending
        v2_data = random_bytes(512)
        v2_hash = sha256(v2_data)
        ls.write_pending_update("calculator.exe", v2_data, 2, v2_hash)

        meta_path = base / "pending_updates" / "calculator.exe.meta"
        meta_v2 = json.loads(meta_path.read_text(encoding="utf-8"))
        check("Pending conține v2 după primul update", meta_v2.get("version") == 2)

        # Primește v3 → suprascrie pending
        v3_data = random_bytes(512)
        v3_hash = sha256(v3_data)
        ls.write_pending_update("calculator.exe", v3_data, 3, v3_hash)

        meta_v3 = json.loads(meta_path.read_text(encoding="utf-8"))
        check("Pending conține v3 după al doilea update", meta_v3.get("version") == 3)
        check(".new conține datele v3", (base / "pending_updates" / "calculator.exe.new").read_bytes() == v3_data)

        # Deblochează și aplică
        ls.unlock_app("calculator.exe")
        process_pending_updates(ls, log_func=print)

        installed = base / "downloads" / "calculator.exe"
        check("Se instalează v3, nu v2", installed.read_bytes() == v3_data)

        state = json.loads((base / "data" / "client_state.json").read_text(encoding="utf-8"))
        check("client_state.json are versiunea 3", state.get("apps", {}).get("calculator.exe", {}).get("version") == 3)


# ---------------------------------------------------------------------------
# Test 8 — Retry worker rulează în background thread
# ---------------------------------------------------------------------------

def test_retry_worker_background() -> None:
    print("\n[TEST 8] Retry worker rulează în background și aplică automat")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        # Instalează v1
        v1_data = random_bytes(512)
        v1_hash = sha256(v1_data)
        ls.write_installed_file("game.exe", v1_data, 1, v1_hash)

        # Pune v2 în pending
        v2_data = random_bytes(512)
        v2_hash = sha256(v2_data)
        ls.write_pending_update("game.exe", v2_data, 2, v2_hash)

        stop_event = threading.Event()
        logs: list[str] = []

        thread = start_retry_worker(ls, stop_event=stop_event, log_func=logs.append)
        check("Thread retry worker pornit", thread.is_alive())

        deadline = time.time() + 8
        applied = False
        while time.time() < deadline:
            installed = base / "downloads" / "game.exe"
            if installed.exists() and installed.read_bytes() == v2_data:
                applied = True
                break
            time.sleep(0.2)

        stop_event.set()
        thread.join(timeout=3)

        check("Retry worker a aplicat update-ul automat în background", applied)
        check("Thread oprit corect după stop_event", not thread.is_alive())


# ---------------------------------------------------------------------------
# Test 9 — Hash mismatch → nu salvează, nu trimite ACK logic
# ---------------------------------------------------------------------------

def test_hash_mismatch_rejected() -> None:
    print("\n[TEST 9] Hash mismatch → fișier respins, nu se salvează")

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ls = make_local_state(base)

        file_data = random_bytes(1024)
        wrong_hash = "0" * 64  # hash greșit intenționat

        header = {
            "action": "FILE_TRANSFER",
            "app_name": "calculator.exe",
            "version": 1,
            "hash": wrong_hash,
            "request_id": 1,
        }

        logs: list[str] = []
        result = process_received_file(ls, header, file_data, log_func=logs.append)

        check("process_received_file returnează False", not result)
        check("Fișier NU salvat în downloads/", not (base / "downloads" / "calculator.exe").exists())
        check("Fișier NU salvat în pending_updates/", not (base / "pending_updates" / "calculator.exe.new").exists())

        mismatch_log = any("hash mismatch" in msg.lower() for msg in logs)
        check("Hash mismatch logat corect", mismatch_log)

        state = json.loads((base / "data" / "client_state.json").read_text(encoding="utf-8"))
        check("client_state.json nu conține aplicația respinsă", "calculator.exe" not in state.get("apps", {}))


# ---------------------------------------------------------------------------
# Sumar final
# ---------------------------------------------------------------------------

def print_summary() -> None:
    print("\n" + "=" * 55)
    print("SUMAR TESTE")
    print("=" * 55)

    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    total = len(_results)

    if failed:
        print(f"\nTeste eșuate ({failed}):")
        for name, ok in _results:
            if not ok:
                print(f"  {FAIL}  {name}")

    print(f"\nRezultat: {passed}/{total} teste trecute")
    if failed == 0:
        print(f"\n{PASS} Toate testele au trecut!")
    else:
        print(f"\n{FAIL} {failed} teste au eșuat.")
    print("=" * 55)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 55)
    print("TEST CLIENT — validare locală fără server")
    print("=" * 55)

    test_direct_install()
    test_pending_when_locked()
    test_retry_applies_after_unlock()
    test_orphan_meta_cleanup()
    test_orphan_new_cleanup()
    test_instance_isolation()
    test_newer_update_overwrites_pending()
    test_retry_worker_background()
    test_hash_mismatch_rejected()

    print_summary()

    failed_count = sum(1 for _, ok in _results if not ok)
    sys.exit(0 if failed_count == 0 else 1)