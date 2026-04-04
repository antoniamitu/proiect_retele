## 1. Network Configuration

**File: `shared/src/config.py`**

```python
HOST = "0.0.0.0"       # server listens on all interfaces (needed for Docker)
PORT = 9000
BUFFER_SIZE = 4096
SOCKET_TIMEOUT = 5      # seconds — used for select() polling interval
FRAME_TIMEOUT = 10      # seconds — max time to read a complete frame once select() triggers
ACK_TIMEOUT = 10        # seconds — max wait for ACK after file transfer
RETRY_INTERVAL = 5      # seconds — client retry worker interval
```

Clients use `SERVER_HOST = "127.0.0.1"` (or the Docker host IP) and `SERVER_PORT = 9000` in their own config.

---

## 2. Protocol Framing

**File: `shared/src/protocol.py`**

Every message on the wire:

```
[4 bytes: JSON header length, big-endian unsigned int]
[N bytes: JSON header, UTF-8 encoded]
[if "file_size" > 0 in JSON: exactly file_size bytes of raw binary data]
```

Rules:
- The 4-byte length covers **only** the JSON portion.
- If the JSON header contains `"file_size"` with a value > 0, the receiver reads exactly that many additional bytes. If `"file_size"` is absent or 0, there is no binary payload.
- Messages that carry binary payload: `FILE_TRANSFER`, `PUSH_UPDATE`. All others have no binary payload.

**Sending:**

```python
import struct, json

def send_message(sock, header_dict, file_data=None):
    header = dict(header_dict)  # shallow copy, never mutate caller's dict
    if file_data is not None:
        header["file_size"] = len(file_data)
    else:
        header.pop("file_size", None)
    json_bytes = json.dumps(header).encode("utf-8")
    sock.sendall(struct.pack("!I", len(json_bytes)))
    sock.sendall(json_bytes)
    if file_data is not None:
        sock.sendall(file_data)
```

**Receiving:**

```python
def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), BUFFER_SIZE))
        if not chunk:
            raise ConnectionError("Socket closed")
        buf.extend(chunk)
    return bytes(buf)

def recv_message(sock):
    raw_length = recv_exact(sock, 4)
    json_length = struct.unpack("!I", raw_length)[0]
    json_bytes = recv_exact(sock, json_length)
    header = json.loads(json_bytes.decode("utf-8"))
    file_data = None
    if header.get("file_size", 0) > 0:
        file_data = recv_exact(sock, header["file_size"])
    return header, file_data
```

**Critical frame-reading rule:** `recv_message()` must never be called without first verifying data is available via `select.select()`. Once `select()` triggers, the caller sets `sock.settimeout(FRAME_TIMEOUT)` inside a `try/finally` block that guarantees `sock.settimeout(None)` is restored regardless of outcome. If `socket.timeout` occurs during frame reading, the connection is considered **compromised and must be closed** — no attempt to resume or save partial data.

**Two-level error separation:**

Protocol-level errors — **close the connection immediately, no ERROR response:**
- `socket.timeout` during frame reading
- `json.JSONDecodeError`
- `UnicodeDecodeError`
- Decoded header is not a `dict`
- `file_size` is present but is not a non-negative `int`

Message-level errors — **send an ERROR response, keep the connection open:**
- Unknown `action`
- Missing required field (`app_name`, `ack_for_request_id`, etc.)
- Invalid `app_name` (contains `/`, `\`, or `..`)
- `APP_NOT_FOUND`
- Unexpected `ACK` (no `pending_transfer` exists)

**`send_error` helper rule:** When sending an ERROR response, use `request_id` from the received header if it exists and is an integer, otherwise use `0`.

These two functions are in `shared/src/protocol.py`. Everyone uses them. Nobody writes custom socket logic.

**Note for future/real projects:** For large files, read/send in chunks to avoid loading entire files into RAM. For this school project with small demo files, the current approach is fine.

---

## 3. Message Types — Complete List

**File: `shared/src/protocol.py`**

```python
# Client -> Server
HELLO = "HELLO"
LIST_APPS = "LIST_APPS"
DOWNLOAD = "DOWNLOAD"
CHECK_UPDATES = "CHECK_UPDATES"
ACK = "ACK"
DISCONNECT = "DISCONNECT"

# Server -> Client
LIST_RESPONSE = "LIST_RESPONSE"
FILE_TRANSFER = "FILE_TRANSFER"
PUSH_UPDATE = "PUSH_UPDATE"
CHECK_UPDATES_RESPONSE = "CHECK_UPDATES_RESPONSE"
ERROR = "ERROR"
```

---

## 4. Universal Message Rules

These apply to **every single message**, no exceptions:

1. **Every message contains `"action"`** — how the receiver identifies the message type.
2. **Every message contains `"request_id"`** — integer.
   - Client-initiated messages: the client increments from 1 (1, 2, 3, ...).
   - Server responses to client requests: echo back the same `request_id`.
   - Server-initiated pushes (`PUSH_UPDATE`): the server has its own counter, starting from 1001, incrementing. Every push gets a unique ID.
3. **`"client_id"` is sent only in HELLO.** After HELLO, the server knows which client owns the socket. Subsequent messages do not include `client_id`.
4. **Every server→client message contains `"status"`**: either `"OK"` or `"ERROR"`.
5. **ACK never receives a response.** The server processes the ACK internally and sends nothing back.
6. **ACK contains `"ack_for_request_id"`** — the `request_id` of the message being acknowledged.
7. **`request_id` validation:** Every message must include `request_id`. If `request_id` is missing, the server responds with `MISSING_FIELD`. If `request_id` exists but is not an integer, the server responds with `INVALID_REQUEST`. In both cases the connection stays open.
8. **No pipelining.** Only one active flow on a socket at a time. The client does not send a new request until the previous flow has finished. After receiving FILE_TRANSFER or PUSH_UPDATE, the next message from the client **must** be the corresponding ACK.
9. **`app_name` validation:** `app_name` must be a simple filename with no path separators (`/`, `\`) and no `..` (double dot). The server responds with `INVALID_REQUEST` for any request with an invalid `app_name`.
10. **Pending transfer violation rule:** If `pending_transfer` is not `None` (the server is waiting for an ACK), the next client message **must** be a valid ACK with `ack_for_request_id` matching `pending_transfer["request_id"]`. Any other message — or an ACK with the wrong `ack_for_request_id` — is treated as `INVALID_REQUEST` and the **connection is closed**. This is the one message-level error that causes connection closure, because the protocol state has become unrecoverable.

---

## 5. HELLO — Mandatory First Message

**Client sends immediately after TCP connect:**

```json
{
  "action": "HELLO",
  "request_id": 1,
  "client_id": "client_mara"
}
```

**Server responds:**

```json
{
  "status": "OK",
  "action": "HELLO",
  "request_id": 1,
  "message": "Welcome, client_mara"
}
```

**Rules:**
- The server rejects any non-HELLO message on a socket that hasn't completed HELLO, responding with ERROR code `CLIENT_NOT_REGISTERED`.
- After HELLO, the server maps this socket to `client_id` in `active_clients`. All subsequent messages on this socket are attributed to that `client_id` automatically.
- **Duplicate client_id:** If `client_mara` is already in `active_clients` (old connection not yet cleaned up), the new connection **wins**. The server closes the old socket and replaces it. See Section 23 for race-condition-safe cleanup.

---

## 6. LIST_APPS

**Client sends:**

```json
{
  "action": "LIST_APPS",
  "request_id": 2
}
```

**Server responds:**

```json
{
  "status": "OK",
  "action": "LIST_RESPONSE",
  "request_id": 2,
  "apps": [
    {
      "name": "calculator.exe",
      "version": 1,
      "hash": "a3f2b8c..."
    },
    {
      "name": "notes.exe",
      "version": 3,
      "hash": "f7d1e9a..."
    },
    {
      "name": "game.exe",
      "version": 1,
      "hash": "b8e4c2d..."
    }
  ]
}
```

---

## 7. DOWNLOAD

**Client sends:**

```json
{
  "action": "DOWNLOAD",
  "request_id": 3,
  "app_name": "calculator.exe"
}
```

**Server responds (header + binary payload):**

```json
{
  "status": "OK",
  "action": "FILE_TRANSFER",
  "request_id": 3,
  "app_name": "calculator.exe",
  "version": 2,
  "hash": "a3f2b8c...",
  "file_size": 102400
}
```
followed by exactly 102400 bytes of binary data.

**Important:** `pending_transfer` is set **after** `send_message()` succeeds, not before. If the send fails, the server must not remain in a false "waiting for ACK" state.

**Client verifies hash, saves file to disk, then sends ACK:**

```json
{
  "action": "ACK",
  "request_id": 4,
  "ack_for_request_id": 3,
  "app_name": "calculator.exe",
  "version": 2
}
```

**No server response to ACK.** The server receives the ACK, registers the download in `downloads_registry.json`, and continues.

The flow is exactly 3 steps:
1. Client sends DOWNLOAD
2. Server sends FILE_TRANSFER + binary
3. Client sends ACK

**ACK is sent only after:** hash verified AND file successfully saved to disk (either to `downloads/` or `pending_updates/`). If either fails, no ACK is sent.

---

## 8. CHECK_UPDATES (on reconnection)

**Client sends:**

```json
{
  "action": "CHECK_UPDATES",
  "request_id": 2,
  "local_apps": [
    {
      "name": "calculator.exe",
      "version": 1,
      "hash": "old_hash..."
    },
    {
      "name": "notes.exe",
      "version": 3,
      "hash": "current_hash..."
    }
  ]
}
```

**Server processing:**

1. Compare each app in `local_apps` against `self.apps` to find newer versions.
2. **Registry resynchronization:** For each app in `local_apps` that exists on the server, the server adds it to `self.downloads[client_id]` if not already present (using `self.downloads.setdefault(client_id, set())`). This repairs lost ACKs: if the client has the app but the server's registry doesn't know, this corrects the state so future pushes work correctly.
3. Save `downloads_registry.json` if anything changed.

**Server responds — only lists apps with newer versions:**

```json
{
  "status": "OK",
  "action": "CHECK_UPDATES_RESPONSE",
  "request_id": 2,
  "updates": [
    {
      "name": "calculator.exe",
      "version": 2,
      "hash": "new_hash..."
    }
  ]
}
```

If no updates, `"updates"` is `[]`.

After receiving this, the client sends a DOWNLOAD for each app in `updates`, one at a time, using the normal DOWNLOAD → FILE_TRANSFER → ACK flow.

---

## 9. PUSH_UPDATE (server push)

When someone publishes a new version, the server pushes to all connected clients who previously downloaded that app. The publisher places a push task in the client's pending pushes dict. The handler thread picks it up **only when there is no pending input from the client and no pending transfer awaiting ACK**, sends it, and handles the ACK when it arrives through the normal recv loop. **The handler never blocks waiting for ACK inline.**

**Server sends (via handler thread, header + binary payload):**

```json
{
  "status": "OK",
  "action": "PUSH_UPDATE",
  "request_id": 1001,
  "app_name": "calculator.exe",
  "version": 3,
  "hash": "newest_hash...",
  "file_size": 103000
}
```
followed by exactly 103000 bytes of binary data.

Each PUSH_UPDATE gets a unique `request_id` from the server's own counter (starting at 1001, incrementing).

**Important:** `pending_transfer` is set **after** `send_message()` succeeds, not before. If the send fails, the handler catches the exception and breaks out of the loop (client disconnected).

**Client verifies hash, saves file to disk, then sends ACK:**

```json
{
  "action": "ACK",
  "request_id": 5,
  "ack_for_request_id": 1001,
  "app_name": "calculator.exe",
  "version": 3
}
```

**No server response to ACK.** The handler receives the ACK through the normal recv loop, matches it via `ack_for_request_id`, and continues. `downloads_registry.json` is **not** modified for pushes — the app was already associated with this client.

**ACK is sent only after:** hash verified AND file successfully saved to disk (to `downloads/` or `pending_updates/`). If either fails, no ACK is sent.

---

## 10. What Happens If ACK Does Not Arrive

After sending FILE_TRANSFER or PUSH_UPDATE, the server does **not** block waiting for ACK. Instead, the handler continues its normal loop. However, if ACK does not arrive within `ACK_TIMEOUT` (10 seconds) — measured from when the file was sent — the server considers the transfer failed:

- The download is **not** registered.
- The server logs the failure.
- The connection is closed.

The handler tracks this with the `pending_transfer` structure (see Section 19).

---

## 11. DISCONNECT

**Client sends before closing:**

```json
{
  "action": "DISCONNECT",
  "request_id": 6
}
```

The server cleans up the active socket but **keeps** the download history. No response is sent — the client closes the socket immediately after.

If the client crashes without sending DISCONNECT, the server detects it via socket error and does the same cleanup.

---

## 12. ERROR Responses

**Format:**

```json
{
  "status": "ERROR",
  "action": "ERROR",
  "request_id": 3,
  "code": "APP_NOT_FOUND",
  "message": "The application 'foo.exe' does not exist."
}
```

`request_id` is taken from the received header if it exists and is an integer, otherwise `0`.

**Complete list of error codes:**

```
INVALID_REQUEST       — unknown action, invalid app_name (contains /, \, or ..),
                        unexpected ACK when no transfer is pending,
                        protocol violation during pending_transfer
                        (wrong message type or wrong ack_for_request_id),
                        or non-integer request_id

MISSING_FIELD         — a required field is missing from an otherwise valid message,
                        including missing request_id

APP_NOT_FOUND         — the requested app_name does not exist on the server

CLIENT_NOT_REGISTERED — any action received on a socket before HELLO has completed

INTERNAL_ERROR        — unexpected server failure
```

**When errors close the connection vs. keep it open:**

Errors that **close** the connection (protocol is unrecoverable) — no ERROR response sent:
- Protocol-level frame corruption: timeout mid-frame, `json.JSONDecodeError`, `UnicodeDecodeError`, non-dict header, invalid `file_size` type or negative value.

Errors that **close** the connection — ERROR response sent first:
- Pending transfer violation: any message other than the correct ACK arrives while `pending_transfer` is set.

Errors that **keep** the connection open — ERROR response sent:
- `MISSING_FIELD` — client can retry with a corrected message. This includes missing `request_id`.
- `APP_NOT_FOUND` — client can try a different app.
- `CLIENT_NOT_REGISTERED` — client can send HELLO.
- `INVALID_REQUEST` for unknown action, invalid `app_name`, unexpected ACK (when no `pending_transfer`), or non-integer `request_id` — connection stays open.
- `INTERNAL_ERROR` — connection stays open.

---

## 13. Hash, Version, and Client Identity

**File: `shared/src/hash_utils.py`**

```python
import hashlib

def compute_hash(file_data: bytes) -> str:
    return hashlib.sha256(file_data).hexdigest()
```

- **Hash:** lowercase hexadecimal SHA-256 (64 characters).
- **Version:** integer starting at 1, incremented by 1 on each publish. Version determines "newer." Hash is for integrity verification on the client.
- **Client IDs:** fixed per client instance — `client_antonia`, `client_mara`, `client_auxeniu`. Defined in each client's config file. Stable across reconnections. Sent **only** in HELLO. After that, the server deduces identity from the socket mapping.

---

## 14. Server Data Structures (in memory)

**File: `server/src/state_manager.py`**

```python
import threading

class StateManager:
    def __init__(self):
        self.lock = threading.Lock()

        # Persistent: app_name -> {"name", "version", "hash", "file_path"}
        self.apps = {}

        # Persistent: client_id -> set of app_names
        # On load from JSON: list -> set
        # On save to JSON: set -> sorted list
        self.downloads = {}

        # Live only: client_id -> socket object
        self.active_clients = {}

        # Live only: client_id -> dict[app_name] -> push_task
        # Only keeps latest version per app
        self.pending_pushes = {}

        # Server-side request_id counter for PUSH_UPDATE messages
        self.server_request_id = 1000  # incremented before each use
```

Key design points:
- **No per-client socket locks.** Only one thread (the handler) ever touches each socket.
- **`pending_pushes`** is a dict of dicts. For each client, it maps `app_name → latest_task`. If the server publishes v2 then v3 quickly, only v3 is kept.
- **`self.downloads` stores sets in memory**, converted to sorted lists when saving to JSON, converted back to sets when loading.
- `self.lock` protects all reads and writes to all structures.
- **Always use `setdefault` when accessing entries that may not exist yet:** `self.downloads.setdefault(client_id, set())` and `self.pending_pushes.setdefault(client_id, {})`.

---

## 15. State Persistence — JSON Files

**Server:**

`server/data/apps_manifest.json`
```json
{
  "calculator.exe": {
    "name": "calculator.exe",
    "version": 2,
    "hash": "a3f2b8c...",
    "file_path": "calculator.exe"
  },
  "notes.exe": {
    "name": "notes.exe",
    "version": 1,
    "hash": "f7d1e9a...",
    "file_path": "notes.exe"
  }
}
```

`file_path` stores **only the filename**. The actual path is built at runtime from a config constant `APPS_DIR`.

`server/data/downloads_registry.json`
```json
{
  "client_mara": ["calculator.exe", "notes.exe"],
  "client_antonia": ["calculator.exe"],
  "client_auxeniu": []
}
```

Lists are **sorted alphabetically** when saved. On load, converted to sets.

**Client:**

`client/data/client_state.json`
```json
{
  "client_id": "client_mara",
  "apps": {
    "calculator.exe": {
      "version": 1,
      "hash": "old_hash..."
    },
    "notes.exe": {
      "version": 3,
      "hash": "current_hash..."
    }
  }
}
```

**`client_state.json` reflects only what is actually installed in `downloads/`.** It is never updated when a file is placed in `pending_updates/`. It is updated only when the file is actually applied to `downloads/`.

**When to save:**
- Server: immediately after registering a download (ACK received for DOWNLOAD), after a publish, and after CHECK_UPDATES resynchronization.
- Client: immediately after a file is written to `downloads/` (either directly or by the retry worker via `os.replace`). Never when writing to `pending_updates/`.

---

## 16. Client-Side Thread Safety

**File: `client/src/local_state.py`**

The client has two threads that can modify local state:
- The main/listener thread (receives files, writes to downloads or pending_updates).
- The retry worker (applies pending updates, writes to downloads).

A single `threading.Lock()` protects:
- All reads/writes to `client_state.json`.
- All file operations in `downloads/` and `pending_updates/`.

```python
import threading

class LocalState:
    def __init__(self):
        self.lock = threading.Lock()
        # ... state loading logic
```

Every file write or state update goes through `with self.lock:`.

---

## 17. Folder Structure

```
project/
├── shared/
│   └── src/
│       ├── config.py
│       ├── protocol.py
│       └── hash_utils.py
├── server/
│   ├── apps/
│   │   ├── calculator.exe
│   │   ├── notes.exe
│   │   └── game.exe
│   ├── data/
│   │   ├── apps_manifest.json
│   │   └── downloads_registry.json
│   ├── src/
│   │   ├── server_main.py
│   │   ├── client_handler.py
│   │   ├── state_manager.py
│   │   └── publisher.py
│   └── Dockerfile
├── client/
│   ├── downloads/
│   ├── pending_updates/
│   ├── data/
│   │   └── client_state.json
│   └── src/
│       ├── main_client.py
│       ├── update_manager.py
│       ├── retry_worker.py
│       └── local_state.py
└── README.md
```

---

## 18. Publishing a New Version

**File: `server/src/publisher.py`**

Triggered by server operator typing `publish calculator.exe` in server console.

Steps:
1. Read the new file from `APPS_DIR/calculator.exe`.
2. Compute SHA-256 hash.
3. Acquire `state_manager.lock`.
4. Increment version by 1.
5. Update `self.apps["calculator.exe"]` with new version and hash.
6. Save `apps_manifest.json` to disk.
7. Get list of client_ids from `self.downloads` who have this app.
8. Increment `self.server_request_id`.
9. For each such client that is in `self.active_clients`: use `self.pending_pushes.setdefault(client_id, {})` then set `self.pending_pushes[client_id]["calculator.exe"]` to a push task containing `{"request_id": ..., "app_name": ..., "version": ..., "hash": ..., "file_data": ...}`. If a previous push for the same app was pending, it is overwritten (only latest version kept).
10. Release `state_manager.lock`.
11. Disconnected clients get the update via CHECK_UPDATES when they reconnect.

The publisher **never touches any socket directly**. It only writes to `pending_pushes`.

---

## 19. Server Concurrency Model — Single Thread Per Socket, Event-Driven

**File: `server/src/server_main.py` and `server/src/client_handler.py`**

- One main thread runs `socket.accept()` in a loop.
- Each new connection spawns a `threading.Thread` running `client_handler.handle_client(sock, addr, state_manager)`.
- **Only this handler thread reads from and writes to that socket. No exceptions.**

**Pending transfer tracking:**

The handler uses a single structured variable:

```python
pending_transfer = None
# When a file is sent successfully, this becomes:
# {
#     "request_id": 3,
#     "kind": "DOWNLOAD" or "PUSH_UPDATE",
#     "app_name": "calculator.exe",
#     "version": 2,
#     "since": time.time()
# }
```

**`pending_transfer` is set only after `send_message()` succeeds.** If the send raises an exception, the handler catches it and breaks (client disconnected), never entering a false "waiting for ACK" state.

**Handler thread loop:**

```python
import select, time, json
import socket as socket_module

def handle_client(sock, addr, state_manager):
    client_id = None
    pending_transfer = None

    while True:
        # 1. Check ACK timeout
        if pending_transfer is not None:
            if time.time() - pending_transfer["since"] > ACK_TIMEOUT:
                log(f"ACK timeout for {client_id}, closing")
                break

        # 2. Check if socket has data available (using select)
        readable, _, _ = select.select([sock], [], [], SOCKET_TIMEOUT)

        if readable:
            # 3. Data available — set temporary frame timeout in try/finally
            try:
                sock.settimeout(FRAME_TIMEOUT)
                header, file_data = recv_message(sock)
            except socket_module.timeout:
                log(f"Incomplete frame from {client_id}, closing")
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                log(f"Corrupted frame from {client_id}, closing")
                break
            except (ConnectionError, ConnectionResetError, OSError):
                break
            finally:
                sock.settimeout(None)

            # 3a. Validate header is a dict
            if not isinstance(header, dict):
                log(f"Non-dict header from {client_id}, closing")
                break

            # 3b. Validate file_size type if present
            if "file_size" in header and (
                not isinstance(header["file_size"], int)
                or header["file_size"] < 0
            ):
                log(f"Invalid file_size from {client_id}, closing")
                break

            # 3c. Validate request_id
            if "request_id" not in header:
                send_error(sock, header, "MISSING_FIELD",
                    "request_id is required")
                continue
            if not isinstance(header["request_id"], int):
                send_error(sock, header, "INVALID_REQUEST",
                    "request_id must be an integer")
                continue

            action = header.get("action")

            # 4. Pending transfer enforcement
            if pending_transfer is not None:
                if (action != "ACK"
                    or header.get("ack_for_request_id")
                       != pending_transfer["request_id"]):
                    send_error(sock, header, "INVALID_REQUEST",
                        "Expected ACK, protocol violation")
                    break  # connection closed

                if pending_transfer["kind"] == "DOWNLOAD":
                    with state_manager.lock:
                        state_manager.downloads.setdefault(
                            client_id, set()
                        ).add(pending_transfer["app_name"])
                        save_downloads_registry()
                # For PUSH_UPDATE: just log, no registry change
                pending_transfer = None
                continue

            # 5. Normal message routing (no pending_transfer)
            if action == "ACK":
                send_error(sock, header, "INVALID_REQUEST",
                    "No transfer pending")
                # Connection stays open

            elif action == "HELLO":
                ...
            elif action == "LIST_APPS":
                ...
            elif action == "DOWNLOAD":
                # validate app_name, prepare response
                try:
                    send_message(sock, file_transfer_header, binary_data)
                except (ConnectionError, ConnectionResetError, OSError):
                    break
                # Set pending_transfer ONLY AFTER send succeeds
                pending_transfer = {
                    "request_id": header["request_id"],
                    "kind": "DOWNLOAD",
                    "app_name": header["app_name"],
                    "version": app_version,
                    "since": time.time()
                }
            elif action == "CHECK_UPDATES":
                ...
            elif action == "DISCONNECT":
                break
            else:
                send_error(sock, header, "INVALID_REQUEST",
                    f"Unknown action: {action}")

        # 6. Only if no client input AND no pending transfer, send a push
        if not readable and pending_transfer is None:
            task = None
            with state_manager.lock:
                pushes = state_manager.pending_pushes.get(
                    client_id, {})
                if pushes:
                    app_name = next(iter(pushes))
                    task = pushes.pop(app_name)
            if task is not None:
                try:
                    send_message(sock, push_header, task["file_data"])
                except (ConnectionError, ConnectionResetError, OSError):
                    break
                # Set pending_transfer ONLY AFTER send succeeds
                pending_transfer = {
                    "request_id": task["request_id"],
                    "kind": "PUSH_UPDATE",
                    "app_name": task["app_name"],
                    "version": task["version"],
                    "since": time.time()
                }

    # Cleanup — race-condition-safe (see Section 23)
    cleanup(sock, client_id, state_manager)
```

**Key properties:**
- `select.select()` checks data availability before calling `recv_message()`.
- Once `select()` triggers, `sock.settimeout(FRAME_TIMEOUT)` is set in a `try/finally` that guarantees `sock.settimeout(None)` is always restored. Protocol-level corruption (timeout mid-frame, bad JSON, non-dict header, invalid `file_size`) causes immediate connection closure with no ERROR response.
- **`request_id` is validated explicitly** before any routing: missing → `MISSING_FIELD`, non-integer → `INVALID_REQUEST`. Both keep the connection open.
- **Pending transfer is strictly enforced.** If `pending_transfer` is set, only a matching ACK is accepted. Anything else triggers ERROR + connection closure.
- **Unexpected ACK** (when `pending_transfer is None`) gets an ERROR response (`INVALID_REQUEST`) but the connection stays open.
- **`pending_transfer` is set only after `send_message()` succeeds.** If the send fails, the exception is caught and the loop breaks — no false state.
- **Client messages are always processed first.** Pushes are only sent when there is no incoming client data and no pending transfer awaiting ACK.
- The handler never blocks waiting for a specific ACK inline. It sends the file, records `pending_transfer`, and continues the loop. The ACK arrives naturally through the recv path.
- Only one pending file transfer at a time per client (enforced by `pending_transfer` gate).
- `pending_transfer` contains `kind`, `app_name`, and `version`, making ACK routing completely unambiguous.
- **`send_error` uses `request_id` from the received header if it exists and is an integer, otherwise `0`.**

---

## 20. Client Update Behavior

**File: `client/src/update_manager.py`**

When the client receives a file (from FILE_TRANSFER or PUSH_UPDATE):

1. Compute hash of received data. Compare with hash in header.
2. **If mismatch:** print error, do NOT save, do NOT send ACK. Done.
3. **If match:** check if `downloads/<app_name>.lock` exists.
4. **No lock:** save using atomic replacement:
   - Write to `downloads/<app_name>.tmp`
   - `os.replace("downloads/<app_name>.tmp", "downloads/<app_name>")`
   - Update `client_state.json` (version and hash now reflect the new file)
   - Send ACK.
5. **Lock exists:**
   - Write binary to `pending_updates/<app_name>.tmp`, then `os.replace("pending_updates/<app_name>.tmp", "pending_updates/<app_name>.new")`
   - Write metadata to `pending_updates/<app_name>.meta.tmp` containing `{"version": 3, "hash": "..."}`, then `os.replace("pending_updates/<app_name>.meta.tmp", "pending_updates/<app_name>.meta")`
   - If `.new` and `.meta` already exist for this app (older pending update), they are **overwritten** — only the latest pending version is kept
   - Do **NOT** update `client_state.json` — the officially installed version remains the old one
   - Send ACK.

**`client_state.json` is updated only when a file is actually placed in `downloads/`.** This ensures that on reconnection, CHECK_UPDATES accurately reports what is truly installed, not what is merely pending.

---

## 21. Simulating "Application is Running"

- `downloads/calculator.exe.lock` exists → app is running, cannot be overwritten.
- `downloads/calculator.exe.lock` absent → app is not running.
- Client console commands: `lock calculator.exe` and `unlock calculator.exe` create/delete the lock file.
- No actual process checking needed.

---

## 22. Retry Worker

**File: `client/src/retry_worker.py`**

A background daemon thread running continuously:

```
every RETRY_INTERVAL (5 seconds):
    acquire local_state.lock

    # First pass: clean up orphaned .meta files (no corresponding .new)
    scan pending_updates/ folder for *.meta files
    for each file like "calculator.exe.meta":
        if "calculator.exe.new" does NOT exist:
            log "[RETRY] WARNING: calculator.exe.meta found without .new, deleting orphaned metadata"
            delete "calculator.exe.meta"

    # Second pass: handle pending updates
    scan pending_updates/ folder for *.new files
    for each file like "calculator.exe.new":

        if "calculator.exe.meta" does NOT exist:
            log "[RETRY] ERROR: calculator.exe.new found without .meta, deleting orphaned binary"
            delete "calculator.exe.new"
            continue

        check if downloads/calculator.exe.lock exists

        if no lock:
            read pending_updates/calculator.exe.meta to get version and hash
            os.replace("pending_updates/calculator.exe.new",
                       "downloads/calculator.exe")
            update client_state.json with version and hash from .meta
            os.remove("pending_updates/calculator.exe.meta")
            print "[RETRY] Applied update for calculator.exe"

        if lock exists:
            print "[RETRY] calculator.exe still locked, retrying..."

    release local_state.lock
```

Key rules:
- Uses `os.replace()` for atomic replacement.
- **This is the only place where `client_state.json` is updated for pending updates.**
- **Orphan handling (two cases):**
  - `.meta` without `.new`: the first pass detects and deletes it. Metadata alone is useless without the binary.
  - `.new` without `.meta`: the second pass detects and deletes it. The binary cannot be safely applied without knowing its version and hash.
- After `os.replace()` succeeds: update `client_state.json`, then **explicitly delete** `calculator.exe.meta`. The `.new` file is already gone (consumed by `os.replace()`). If `.meta` is left behind, a future update that fails before writing its own `.meta` could cause the worker to read stale version/hash data.

---

## 23. Connection Handling and Race-Condition-Safe Cleanup

**No heartbeat.** Disconnection detected via `ConnectionError`, `ConnectionResetError`, or zero bytes.

**On disconnect detection (server-side cleanup):**

```python
def cleanup(sock, client_id, state_manager):
    with state_manager.lock:
        # Only remove if this socket is still the active one
        if state_manager.active_clients.get(client_id) is sock:
            del state_manager.active_clients[client_id]
            state_manager.pending_pushes.pop(client_id, None)
    sock.close()
    # Download history is NEVER removed
```

**The `is sock` check is critical.** If a new connection from the same `client_id` has already replaced this socket in `active_clients`, the old handler must **not** remove the new entry. The identity comparison ensures only the rightful owner cleans up.

**On reconnection:** Client sends HELLO (same `client_id`). If old socket still in `active_clients`, the new one replaces it (old socket closed by HELLO handler). Then client sends CHECK_UPDATES, which also resynchronizes the download registry.

---

## 24. Test Data

Three pre-loaded applications:

```
calculator.exe    version 1    1 KB of random bytes
notes.exe         version 1    5 KB of random bytes
game.exe          version 1    20 KB of random bytes
```

Generate with:

```python
import os
os.makedirs("server/apps", exist_ok=True)
with open("server/apps/calculator.exe", "wb") as f:
    f.write(os.urandom(1024))
with open("server/apps/notes.exe", "wb") as f:
    f.write(os.urandom(5120))
with open("server/apps/game.exe", "wb") as f:
    f.write(os.urandom(20480))
```

---

## 25. Demo Scenarios

```
 #   Scenario                                                What happens
───  ──────────────────────────────────────────────────────   ──────────────────────────────────────────────
 1   Start server in Docker                                  docker build + docker run
 2   Connect client_mara and client_antonia                  Both send HELLO
 3   Both request LIST_APPS                                  See all 3 apps
 4   client_mara downloads calculator.exe                    DOWNLOAD -> FILE_TRANSFER -> ACK
 5   client_antonia downloads calculator.exe and notes.exe   Same flow, twice
 6   Server publishes calculator.exe v2                      Operator types "publish calculator.exe"
 7   Both mara and antonia get PUSH_UPDATE                   Sent via handler threads, ACK through recv loop
 8   client_mara downloads notes.exe, locks it,              Update -> pending_updates (.new + .meta),
     server publishes notes.exe v2                           client_state.json still v1, retry applies
                                                             after unlock, updates state, deletes .meta
 9   client_auxeniu connects, downloads calculator.exe       Gets v2 directly
10   client_mara disconnects, server publishes calc v3       mara is offline
11   client_mara reconnects, CHECK_UPDATES                   Server resynchronizes registry, sends v3
```

---
