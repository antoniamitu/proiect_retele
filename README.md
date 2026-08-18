# Client–Server Application Distribution and Update System

A team project implementing a **TCP client–server system for application distribution, version management, and automatic updates**.

The system allows multiple clients to connect to a central server, view available applications, download files, receive new versions automatically, and synchronize their local state after reconnecting.

The project implements a custom communication protocol over TCP sockets, binary file transfers, acknowledgment-based transfer validation, SHA-256 integrity checks, concurrent client handling, persistent state management, automatic update delivery, delayed updates for locked applications, and Docker-based server deployment.

---

## Main Features

### Client–Server Communication

The system uses TCP sockets for communication between a central server and multiple clients.

The server:

* accepts multiple client connections;
* maintains the list of available applications and their versions;
* handles application downloads;
* tracks which applications were downloaded by each client;
* publishes new application versions;
* automatically pushes updates to connected clients;
* synchronizes offline clients when they reconnect.

The clients:

* register with the server using a unique `client_id`;
* request the list of available applications;
* download applications;
* verify file integrity;
* receive pushed updates;
* store application state locally;
* detect available updates after reconnecting;
* delay updates when an application is currently locked.

---

## Technologies Used

* Python 3
* TCP sockets
* Python `threading`
* Python `select`
* JSON
* SHA-256 hashing
* File system operations
* Docker
* Docker Compose

The Docker server image is based on:

```text
python:3.12-slim
```

---

## Project Structure

```text
project/
├── shared/
│   └── src/
│       ├── config.py
│       ├── protocol.py
│       └── hash_utils.py
│
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
│
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
│
├── client_instances/
├── tests/
│   ├── test_client_retry.py
│   ├── validate_two_instances.py
│   └── validate_reconnect.py
│
├── scripts/
│   ├── run_demo.ps1
│   └── run_demo.sh
│
├── generate_demo_apps.py
├── docker-compose.yml
└── README.md
```

---

## Network Configuration

The main network configuration is defined in:

```text
shared/src/config.py
```

Default server configuration:

```python
HOST = "0.0.0.0"
PORT = 9000
BUFFER_SIZE = 4096
SOCKET_TIMEOUT = 5
FRAME_TIMEOUT = 10
ACK_TIMEOUT = 10
RETRY_INTERVAL = 5
```

The server listens on all interfaces on port `9000`.

Clients connect by default to:

```text
127.0.0.1:9000
```

or to the corresponding Docker host address.

---

## Communication Protocol

The project implements a custom application-level protocol on top of TCP.

Every message has the following structure:

```text
[4 bytes: JSON header length]
[N bytes: JSON header encoded as UTF-8]
[optional binary payload]
```

The first four bytes contain the JSON header length as a **big-endian unsigned integer**.

If the header contains a positive `file_size`, exactly that number of binary bytes follows the JSON header.

Binary payloads are used for:

* `FILE_TRANSFER`
* `PUSH_UPDATE`

The shared protocol implementation is located in:

```text
shared/src/protocol.py
```

---

## Message Types

### Client → Server

```text
HELLO
LIST_APPS
DOWNLOAD
CHECK_UPDATES
ACK
DISCONNECT
```

### Server → Client

```text
LIST_RESPONSE
FILE_TRANSFER
PUSH_UPDATE
CHECK_UPDATES_RESPONSE
ERROR
```

Every message contains:

```text
action
request_id
```

Server responses also contain a status:

```text
OK
ERROR
```

The `client_id` is transmitted during the initial `HELLO` exchange. After registration, the server associates the client identity with its socket.

---

## Client Registration

Immediately after establishing the TCP connection, the client sends a `HELLO` message.

Example:

```json
{
  "action": "HELLO",
  "request_id": 1,
  "client_id": "client_mara"
}
```

The server registers the client and responds with a confirmation.

If another connection using the same `client_id` already exists, the new connection replaces the old one.

---

## Application Listing

Clients can request the applications currently available on the server.

Example response:

```json
{
  "status": "OK",
  "action": "LIST_RESPONSE",
  "request_id": 2,
  "apps": [
    {
      "name": "calculator.exe",
      "version": 1,
      "hash": "..."
    },
    {
      "name": "notes.exe",
      "version": 1,
      "hash": "..."
    },
    {
      "name": "game.exe",
      "version": 1,
      "hash": "..."
    }
  ]
}
```

Each application is represented by:

* name;
* version;
* SHA-256 hash.

---

## Application Download

A client can request an application using `DOWNLOAD`.

The transfer follows three main steps:

```text
Client                         Server
  |                               |
  | -------- DOWNLOAD ----------> |
  |                               |
  | <--- FILE_TRANSFER + file --- |
  |                               |
  | ---------- ACK -------------> |
```

The server sends a JSON header followed by the binary file contents.

After receiving the file, the client:

1. computes its SHA-256 hash;
2. compares it with the hash provided by the server;
3. saves the file;
4. sends an `ACK`.

The acknowledgment is sent only if both integrity verification and file storage succeed.

---

## File Integrity

SHA-256 is used to verify transferred files.

The hash implementation is shared between the client and server.

Hashes are represented as lowercase hexadecimal strings.

The version number determines whether an application is newer, while the hash is used to verify file integrity.

---

## Application Versions

Each application starts at:

```text
version 1
```

When the server operator publishes another version, its version number is incremented.

The application manifest is persisted in:

```text
server/data/apps_manifest.json
```

It stores information such as:

```json
{
  "calculator.exe": {
    "name": "calculator.exe",
    "version": 2,
    "hash": "...",
    "file_path": "calculator.exe"
  }
}
```

---

## Publishing Updates

A new version can be published from the server console.

First, the application file in:

```text
server/apps/
```

is replaced with the new version.

Then the operator runs:

```text
publish calculator.exe
```

The server:

1. reads the new file;
2. computes its SHA-256 hash;
3. increments the application version;
4. updates the application manifest;
5. identifies clients that previously downloaded the application;
6. queues an update for connected clients.

The publisher does not directly access client sockets. Updates are placed into the corresponding client's pending push queue and are sent by the client's own handler thread.

---

## Automatic Push Updates

Connected clients that previously downloaded an application can receive a new version automatically through:

```text
PUSH_UPDATE
```

The pushed message contains:

* application name;
* new version;
* SHA-256 hash;
* binary file size;
* binary file contents.

After successfully storing and verifying the update, the client sends an `ACK`.

Only one file transfer can be pending for a client at a time.

If multiple versions of the same application are published before a pending push is sent, only the newest pending version is retained.

---

## Offline Clients and Reconnection

Clients that are offline when a new version is published receive the update after reconnecting.

After the initial `HELLO`, the client automatically sends:

```text
CHECK_UPDATES
```

with information about the applications currently installed locally.

The server compares the client's versions with the current server versions and returns only applications for which newer versions exist.

The client then downloads the updates sequentially using the normal:

```text
DOWNLOAD → FILE_TRANSFER → ACK
```

flow.

`CHECK_UPDATES` also allows the server to resynchronize its download registry with the applications actually installed by the client.

---

## Locked Applications and Pending Updates

The project simulates an application currently being used through `.lock` files.

For example:

```text
downloads/calculator.exe.lock
```

indicates that `calculator.exe` is currently locked.

If an update arrives while the application is locked, the installed file is not overwritten.

Instead, the update is stored as:

```text
pending_updates/calculator.exe.new
pending_updates/calculator.exe.meta
```

The `.new` file contains the new binary, while `.meta` contains its version and hash.

The installed version recorded in `client_state.json` remains unchanged until the update is actually applied.

---

## Retry Worker

Each client runs a background retry worker.

The worker checks pending updates every:

```text
5 seconds
```

If the application is no longer locked, the pending update is applied using:

```python
os.replace()
```

The local application state is then updated.

The retry worker also detects and removes inconsistent pending-update states:

* `.meta` without `.new`;
* `.new` without `.meta`.

This prevents corrupted or incomplete pending updates from being applied.

---

## Persistent State

### Server State

The server maintains:

```text
server/data/apps_manifest.json
server/data/downloads_registry.json
```

`apps_manifest.json` stores application versions and hashes.

`downloads_registry.json` records which applications were downloaded by each client.

Example:

```json
{
  "client_mara": [
    "calculator.exe",
    "notes.exe"
  ],
  "client_antonia": [
    "calculator.exe"
  ]
}
```

---

### Client State

Each client maintains its own:

```text
client_state.json
```

Example:

```json
{
  "client_id": "client_mara",
  "apps": {
    "calculator.exe": {
      "version": 1,
      "hash": "..."
    }
  }
}
```

The state contains only applications that are actually installed.

Updates waiting in `pending_updates/` are not considered installed and therefore do not modify this file.

---

## Multiple Client Instances

Different clients use isolated local directories:

```text
client_instances/<instance_name>/
├── downloads/
├── pending_updates/
└── data/
    └── client_state.json
```

This provides independent:

* downloaded files;
* pending updates;
* application locks;
* local state.

Multiple client instances can therefore communicate with the same server without interfering with one another.

---

## Server Concurrency

The server supports multiple simultaneous clients using Python threads.

The architecture uses:

* one accept loop for incoming TCP connections;
* one handler thread for each connected client.

A key design rule is that only the corresponding handler thread reads from and writes to a client's socket.

Shared server state is protected using:

```python
threading.Lock()
```

The server maintains:

* registered applications;
* download history;
* active client sockets;
* pending push updates;
* server-side request IDs.

Socket availability is checked with:

```python
select.select()
```

before attempting to read a complete message frame.

---

## Timeouts and Error Handling

The protocol distinguishes between frame-level and message-level errors.

Frame-level problems such as:

* incomplete frames;
* malformed JSON;
* invalid UTF-8;
* invalid binary payload sizes;

cause the connection to be closed because the message stream can no longer be considered synchronized.

Message-level errors produce structured `ERROR` responses.

Examples include:

```text
INVALID_REQUEST
MISSING_FIELD
APP_NOT_FOUND
CLIENT_NOT_REGISTERED
INTERNAL_ERROR
```

After sending a file, the server also expects an acknowledgment within the configured ACK timeout.

If the expected `ACK` is not received, the transfer is considered unsuccessful and the connection is closed.

---

## Atomic File Updates

Temporary files and `os.replace()` are used when installing or applying updates.

For a normal installation, the file is first written to:

```text
downloads/<app_name>.tmp
```

and then atomically moved to:

```text
downloads/<app_name>
```

Pending updates use the same approach before producing their final `.new` and `.meta` files.

This reduces the risk of leaving partially written installed files.

---

## Running the Server Locally

Run the following command from the repository root:

```bash
python -m server.src.server_main
```

The server listens on:

```text
0.0.0.0:9000
```

Available server commands:

```text
help
publish <app_name>
apps
clients
exit
```

---

## Running the Server with Docker

Build the image from the repository root:

```bash
docker build -f server/Dockerfile -t app-store-server .
```

Run the container:

```bash
docker run --rm -it -p 9000:9000 \
  -v ./server/data:/app/server/data \
  -v ./server/apps:/app/server/apps \
  app-store-server
```

The project can also be started using Docker Compose:

```bash
docker compose up --build
```

The mounted volumes preserve the server application files and JSON state outside the container.

---

## Running a Client

Example:

```bash
python -m client.src.main_client --client-id client_mara --client-instance client_mara
```

Multiple isolated clients can be started using different IDs and instance names:

```bash
python -m client.src.main_client --client-id client_mara --client-instance client_mara
python -m client.src.main_client --client-id client_antonia --client-instance client_antonia
python -m client.src.main_client --client-id client_auxeniu --client-instance client_auxeniu
```

The server address and port can also be specified explicitly:

```bash
python -m client.src.main_client \
  --client-id client_mara \
  --client-instance client_mara \
  --host 127.0.0.1 \
  --port 9000
```

---

## Client Commands

The interactive client provides the following commands:

```text
help
list
download <app_name>
check
lock <app_name>
unlock <app_name>
state
pending
exit
```

Their roles are:

| Command               | Description                                   |
| --------------------- | --------------------------------------------- |
| `help`                | Displays the available commands               |
| `list`                | Requests the list of server applications      |
| `download <app_name>` | Downloads an application                      |
| `check`               | Checks for newer versions and downloads them  |
| `lock <app_name>`     | Simulates an application currently being used |
| `unlock <app_name>`   | Removes the application lock                  |
| `state`               | Displays locally installed applications       |
| `pending`             | Displays pending updates                      |
| `exit`                | Disconnects the client                        |

---

## Demo Applications

The repository contains three demo applications:

```text
calculator.exe
notes.exe
game.exe
```

Their initial sizes are:

| Application      |  Size |
| ---------------- | ----: |
| `calculator.exe` |  1 KB |
| `notes.exe`      |  5 KB |
| `game.exe`       | 20 KB |

They are binary demo files used to test application distribution and update behavior.

They can be regenerated with:

```bash
python generate_demo_apps.py
```

The script also resets:

```text
server/data/apps_manifest.json
server/data/downloads_registry.json
```

After running it, the server should be restarted so that the manifest is rebuilt from the generated application files.

---

## Validation and Testing

The project includes dedicated validation scripts for the main client-side and networking scenarios.

### Client update and retry validation

```bash
python tests/test_client_retry.py
```

Covers:

* direct installation;
* pending updates;
* locked applications;
* retry behavior;
* orphaned update cleanup;
* hash verification.

### Multiple client instances

```bash
python tests/validate_two_instances.py
```

Covers:

* simultaneous clients;
* directory isolation;
* independent client state.

### Reconnection

```bash
python tests/validate_reconnect.py
```

Covers:

* application download;
* client disconnection;
* publishing a new version while the client is offline;
* reconnection;
* automatic update synchronization.

---

## Main Concepts Implemented

The project demonstrates:

* TCP client–server communication;
* a custom application-level protocol;
* binary file transfer;
* message framing;
* request/response correlation using request IDs;
* acknowledgments for completed file transfers;
* SHA-256 integrity verification;
* application version management;
* server-initiated push updates;
* offline update synchronization;
* concurrent client handling;
* shared-state synchronization using locks;
* event-driven socket handling using `select`;
* persistent server and client state using JSON;
* atomic file replacement;
* delayed updates for locked applications;
* background retry processing;
* multiple isolated client instances;
* protocol and connection error handling;
* Docker-based server deployment.

---

## Authors

Team project developed by:

* **Mazâlu Mara**
* **Miclescu Razvan-Auxeniu**
* **Mitu Ana-Maria-Antonia**
