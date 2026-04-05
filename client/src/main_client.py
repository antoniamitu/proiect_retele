from __future__ import annotations

import argparse
import queue
import select
import socket
import threading
from pathlib import Path
from typing import Any

from client.src.local_state import LocalState
from client.src.retry_worker import start_retry_worker
from client.src.update_manager import build_ack_message, process_received_file
from shared.src.config import (
    FRAME_TIMEOUT,
    SERVER_HOST,
    SERVER_PORT,
    SOCKET_TIMEOUT,
    resolve_client_dir,
)
from shared.src.protocol import (
    CHECK_UPDATES,
    CHECK_UPDATES_RESPONSE,
    DISCONNECT,
    DOWNLOAD,
    ERROR,
    FILE_TRANSFER,
    HELLO,
    LIST_APPS,
    LIST_RESPONSE,
    PUSH_UPDATE,
    ProtocolFrameError,
    recv_message,
    send_message,
)


class ConsoleClient:
    def __init__(
        self,
        client_id: str,
        host: str,
        port: int,
        local_base_dir: Path | None = None,
    ) -> None:
        self.client_id = client_id
        self.host = host
        self.port = port

        self.local_state = LocalState(client_id, base_dir=local_base_dir)

        self.sock: socket.socket | None = None
        self.send_lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.pending_lock = threading.Lock()

        self.request_counter = 0
        self.pending_responses: dict[int, queue.Queue] = {}

        self.stop_event = threading.Event()
        self.retry_stop_event = threading.Event()

        self.listener_thread: threading.Thread | None = None
        self.retry_thread: threading.Thread | None = None

    def log(self, message: str) -> None:
        print(f"[CLIENT {self.client_id}] {message}")

    def _next_request_id(self) -> int:
        with self.request_lock:
            self.request_counter += 1
            return self.request_counter

    def _register_waiter(self, request_id: int) -> queue.Queue:
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending_responses[request_id] = waiter
        return waiter

    def _pop_waiter(self, request_id: int) -> queue.Queue | None:
        with self.pending_lock:
            return self.pending_responses.pop(request_id, None)

    def _deliver_response(self, request_id: int, payload: dict[str, Any]) -> None:
        waiter = self._pop_waiter(request_id)
        if waiter is not None:
            waiter.put(payload)

    def _fail_all_waiters(self, reason: str) -> None:
        with self.pending_lock:
            items = list(self.pending_responses.items())
            self.pending_responses.clear()

        for _, waiter in items:
            waiter.put(
                {
                    "status": "ERROR",
                    "action": ERROR,
                    "request_id": 0,
                    "code": "CONNECTION_CLOSED",
                    "message": reason,
                }
            )

    def _send(self, header: dict[str, Any], file_data: bytes | None = None) -> None:
        if self.sock is None:
            raise ConnectionError("Client is not connected")

        with self.send_lock:
            send_message(self.sock, header, file_data)

    def request(self, action: str, timeout: float = 20.0, **payload: Any) -> dict[str, Any]:
        request_id = self._next_request_id()
        waiter = self._register_waiter(request_id)

        header = {
            "action": action,
            "request_id": request_id,
            **payload,
        }

        try:
            self._send(header)
        except OSError as exc:
            self._pop_waiter(request_id)
            raise ConnectionError(f"Failed to send {action}") from exc

        try:
            response = waiter.get(timeout=timeout)
            return response
        except queue.Empty as exc:
            self._pop_waiter(request_id)
            raise TimeoutError(f"Timed out waiting for response to {action}") from exc

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))

        self.listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
            name=f"listener-{self.client_id}",
        )
        self.listener_thread.start()

        hello_response = self.request(HELLO, client_id=self.client_id)
        if hello_response.get("status") != "OK":
            raise RuntimeError(f"HELLO failed: {hello_response}")

        self.log("Connected successfully")

        self.retry_thread = start_retry_worker(
            self.local_state,
            stop_event=self.retry_stop_event,
            log_func=self.log,
        )

        self.check_updates_and_download()

    def disconnect(self) -> None:
        current = threading.current_thread()

        if not self.stop_event.is_set():
            try:
                disconnect_header = {
                    "action": DISCONNECT,
                    "request_id": self._next_request_id(),
                }
                self._send(disconnect_header)
            except Exception:
                pass

        self.stop_event.set()
        self.retry_stop_event.set()

        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        if self.listener_thread is not None and self.listener_thread.is_alive():
            if self.listener_thread is not current:
                self.listener_thread.join(timeout=SOCKET_TIMEOUT + 1)

        if self.retry_thread is not None and self.retry_thread.is_alive():
            if self.retry_thread is not current:
                self.retry_thread.join(timeout=1.0)

        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _listener_loop(self) -> None:
        assert self.sock is not None
        sock = self.sock

        try:
            while not self.stop_event.is_set():
                try:
                    readable, _, _ = select.select([sock], [], [], SOCKET_TIMEOUT)
                except OSError:
                    break

                if not readable:
                    continue

                try:
                    sock.settimeout(FRAME_TIMEOUT)
                    header, file_data = recv_message(sock)
                except ProtocolFrameError as exc:
                    self.log(f"Protocol frame error: {exc}")
                    break
                except socket.timeout:
                    self.log("Incomplete frame received. Closing.")
                    break
                except (ConnectionError, ConnectionResetError, OSError):
                    break
                finally:
                    try:
                        sock.settimeout(None)
                    except OSError:
                        pass

                if not isinstance(header, dict):
                    self.log("Invalid header received. Closing.")
                    break

                request_id = header.get("request_id")
                action = header.get("action")

                if action in {HELLO, LIST_RESPONSE, CHECK_UPDATES_RESPONSE, ERROR}:
                    if isinstance(request_id, int):
                        self._deliver_response(request_id, header)
                    continue

                if action == FILE_TRANSFER:
                    success = process_received_file(
                        self.local_state,
                        header,
                        file_data,
                        log_func=self.log,
                    )

                    if success:
                        if not isinstance(request_id, int):
                            self.log("FILE_TRANSFER missing valid request_id; cannot ACK")
                            break

                        try:
                            ack_message = build_ack_message(
                                request_id=self._next_request_id(),
                                ack_for_request_id=request_id,
                                app_name=header["app_name"],
                                version=header["version"],
                            )
                            self._send(ack_message)
                        except Exception:
                            break

                    response_payload = dict(header)
                    response_payload["transfer_saved"] = success

                    if isinstance(request_id, int):
                        self._deliver_response(request_id, response_payload)
                    continue

                if action == PUSH_UPDATE:
                    success = process_received_file(
                        self.local_state,
                        header,
                        file_data,
                        log_func=self.log,
                    )

                    if success and isinstance(request_id, int):
                        try:
                            ack_message = build_ack_message(
                                request_id=self._next_request_id(),
                                ack_for_request_id=request_id,
                                app_name=header["app_name"],
                                version=header["version"],
                            )
                            self._send(ack_message)
                        except Exception:
                            break
                    continue

                self.log(f"Unknown server action received: {action}")
                continue

        finally:
            self.stop_event.set()
            self.retry_stop_event.set()
            self._fail_all_waiters("Connection closed")

            try:
                sock.close()
            except OSError:
                pass

            self.log("Listener stopped")

    def list_apps(self) -> None:
        response = self.request(LIST_APPS)
        if response.get("status") != "OK":
            self.log(str(response))
            return

        apps = response.get("apps", [])
        if not apps:
            self.log("No apps available.")
            return

        print("\nAvailable apps:")
        for app in apps:
            print(f"- {app['name']} | version={app['version']} | hash={app['hash']}")
        print()

    def download_app(self, app_name: str) -> None:
        response = self.request(
            DOWNLOAD,
            timeout=30.0,
            app_name=app_name,
        )

        if response.get("status") == "ERROR":
            self.log(str(response))
            return

        if response.get("transfer_saved"):
            self.log(f"Download completed for {app_name}")
        else:
            self.log(f"Download failed for {app_name}")

    def check_updates_and_download(self) -> None:
        response = self.request(
            CHECK_UPDATES,
            local_apps=self.local_state.get_local_apps_payload(),
        )

        if response.get("status") != "OK":
            self.log(f"CHECK_UPDATES failed: {response}")
            return

        updates = response.get("updates", [])
        if not updates:
            self.log("No updates available.")
            return

        self.log(f"Updates available: {[item['name'] for item in updates]}")
        for item in updates:
            self.download_app(item["name"])

    def show_state(self) -> None:
        with self.local_state.lock:
            print("\nInstalled state:")
            print(self.local_state.state)
            print()

    def show_pending(self) -> None:
        pending_new = self.local_state.list_pending_app_names()
        pending_meta = self.local_state.list_pending_meta_app_names()

        print("\nPending .new files:", pending_new)
        print("Pending .meta files:", pending_meta)
        print()

    def lock_app(self, app_name: str) -> None:
        self.local_state.lock_app(app_name)
        self.log(f"Locked {app_name}")

    def unlock_app(self, app_name: str) -> None:
        self.local_state.unlock_app(app_name)
        self.log(f"Unlocked {app_name}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remote update client")
    parser.add_argument("--client-id", required=True, help="Unique client identifier")
    parser.add_argument("--host", default=SERVER_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="Server port")
    parser.add_argument(
        "--client-instance",
        help="Optional local storage instance name; defaults to client-id",
    )
    parser.add_argument(
        "--client-dir",
        help="Optional explicit local storage directory",
    )
    return parser


def print_help() -> None:
    print(
        "\nAvailable client commands:\n"
        "  help                Show this help\n"
        "  list                Request app list from server\n"
        "  download <app>      Download one app\n"
        "  check               Check for updates and download them\n"
        "  lock <app>          Simulate app running\n"
        "  unlock <app>        Remove app lock\n"
        "  state               Show installed local state\n"
        "  pending             Show pending update files\n"
        "  exit                Disconnect and quit\n"
    )


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.client_dir:
        local_base_dir = Path(args.client_dir)
    else:
        instance_name = args.client_instance or args.client_id
        local_base_dir = resolve_client_dir(instance_name)

    client = ConsoleClient(
        args.client_id,
        args.host,
        args.port,
        local_base_dir=local_base_dir,
    )

    try:
        client.connect()
        print_help()

        while not client.stop_event.is_set():
            try:
                command = input(f"{args.client_id}> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not command:
                continue

            if command == "help":
                print_help()
                continue

            if command == "list":
                client.list_apps()
                continue

            if command == "check":
                client.check_updates_and_download()
                continue

            if command == "state":
                client.show_state()
                continue

            if command == "pending":
                client.show_pending()
                continue

            if command.startswith("download "):
                app_name = command[len("download ") :].strip()
                client.download_app(app_name)
                continue

            if command.startswith("lock "):
                app_name = command[len("lock ") :].strip()
                client.lock_app(app_name)
                continue

            if command.startswith("unlock "):
                app_name = command[len("unlock ") :].strip()
                client.unlock_app(app_name)
                continue

            if command in {"exit", "quit"}:
                break

            print("Unknown command. Type 'help' for available commands.")

    finally:
        client.disconnect()


if __name__ == "__main__":
    main()