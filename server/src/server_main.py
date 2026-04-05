from __future__ import annotations

import socket
import threading

from shared.src.config import HOST, PORT, SERVER_APPS_DIR
from shared.src.hash_utils import compute_file_hash
from server.src.client_handler import handle_client, log
from server.src.publisher import publish_app
from server.src.state_manager import StateManager


def bootstrap_apps_from_disk(state_manager: StateManager) -> None:
    """
    If apps exist on disk but not yet in manifest, register them as version 1.
    This helps on first startup.
    """
    discovered_apps: list[tuple[str, str]] = []

    for path in SERVER_APPS_DIR.glob("*"):
        if not path.is_file():
            continue
        discovered_apps.append((path.name, compute_file_hash(path)))

    changed = False

    with state_manager.lock:
        for app_name, hash_value in discovered_apps:
            if app_name in state_manager.apps:
                continue

            state_manager.apps[app_name] = {
                "name": app_name,
                "version": 1,
                "hash": hash_value,
                "file_path": app_name,
            }
            changed = True

        if changed:
            state_manager._save_apps_manifest_unlocked()

    if changed:
        log("Bootstrapped manifest from files in server/apps")


def accept_loop(
    server_socket: socket.socket,
    state_manager: StateManager,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            client_sock, addr = server_socket.accept()
        except OSError:
            break

        thread = threading.Thread(
            target=handle_client,
            args=(client_sock, addr, state_manager),
            daemon=True,
            name=f"client-handler-{addr}",
        )
        thread.start()


def print_help() -> None:
    print(
        "\nAvailable commands:\n"
        "  help                 Show this help\n"
        "  publish <app_name>   Publish a new version of an app already overwritten on disk\n"
        "  apps                 Show server app manifest\n"
        "  clients              Show currently connected clients\n"
        "  exit                 Stop server\n"
    )


def command_loop(state_manager: StateManager, stop_event: threading.Event) -> None:
    print_help()

    while not stop_event.is_set():
        try:
            command = input("server> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            stop_event.set()
            break

        if not command:
            continue

        if command == "help":
            print_help()
            continue

        if command == "apps":
            with state_manager.lock:
                if not state_manager.apps:
                    print("No apps registered.")
                else:
                    for app_name, record in sorted(state_manager.apps.items()):
                        print(
                            f"{app_name} | version={record['version']} | hash={record['hash']}"
                        )
            continue

        if command == "clients":
            with state_manager.lock:
                clients = sorted(state_manager.active_clients.keys())

            if not clients:
                print("No active clients.")
            else:
                for client_id in clients:
                    print(client_id)
            continue

        if command in {"exit", "quit"}:
            stop_event.set()
            break

        if command.startswith("publish "):
            app_name = command[len("publish ") :].strip()
            try:
                result = publish_app(state_manager, app_name)
                print(
                    f"Published {result['app_name']} v{result['version']} "
                    f"for clients: {result['queued_for_clients']}"
                )
            except Exception as exc:
                print(f"[PUBLISH ERROR] {exc}")
            continue

        print("Unknown command. Type 'help' for available commands.")


def main() -> None:
    state_manager = StateManager()
    bootstrap_apps_from_disk(state_manager)

    stop_event = threading.Event()

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen()

    log(f"Server listening on {HOST}:{PORT}")

    accept_thread = threading.Thread(
        target=accept_loop,
        args=(server_socket, state_manager, stop_event),
        daemon=True,
        name="accept-loop",
    )
    accept_thread.start()

    try:
        command_loop(state_manager, stop_event)
    finally:
        stop_event.set()

        try:
            server_socket.close()
        except OSError:
            pass

        log("Server stopped.")


if __name__ == "__main__":
    main()