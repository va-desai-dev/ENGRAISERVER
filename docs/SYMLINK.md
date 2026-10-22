# SymLink v1 — paired application channel

SymLink connects ENGRAI to ENGRAISERVER through an authenticated, end-to-end
encrypted application session. It uses an existing reachable LAN, private VPN,
or WebSocket-capable tunnel. No account, hosted discovery service, or networking
provider is required by this protocol. Only the two endpoints have session keys.
An intermediary can still observe addresses, timing, sizes, and disconnect traffic.

This is the initial server implementation, not a completed mesh or an audited
security release. It implements pairing, revocation, text-model listing, and
streaming chat. The native ENGRAI client must implement the contract below before
it can scan these invitations. This repository does not contain that client.
Automatic discovery/NAT traversal, image operations, transfers, multi-server
routing, and multiplexed requests are not part of v1.

## Pair a device

Open **Profile & API Keys → ENGRAI SymLink**, enter a device name and the server
origin reachable from that device, and select **Create pairing code**. For a LAN
address, use the server's address rather than the phone's `localhost`. Existing
tunnels may supply an HTTPS origin. Creating an invitation never opens ports,
starts a tunnel, changes the gateway listener, or contacts the supplied address.

The QR code is generated locally. It expires after five minutes, accepts one
device, and is cancelled when the panel closes. Keep the code private until used.
The server records the enrolled client's public key, not the invitation secret.
**Disconnect device** revokes access and terminates its active sessions, including
generation in progress. A copied invitation cannot enroll a second device after
the first enrollment commits. Possession of an enrolled device's private key
still permits impersonation; pairing authenticates keys, not an app binary or OS.

The management interface uses the existing administrator authentication. Use
loopback or a trusted HTTPS/private connection to administer it: the invitation
is secret material. SymLink's inner encryption does not protect the separate
management HTTP exchange. The QR fragment is never sent as an HTTP query or path.

## Transport and encodings

The server is the Noise **responder**, and ENGRAI is the **initiator**. Connect
using WebSocket subprotocol `engrai.symlink.v1`. Each WebSocket **binary message**
contains exactly one Noise handshake or transport message. Text messages are
rejected. Never coalesce multiple Noise messages into one WebSocket message.
Disable WebSocket compression. Preserve message order.

Use base64url without padding for 32-byte keys (43 ASCII characters). Application
JSON is UTF-8. Response byte chunks use ordinary padded base64. Maximum Noise
ciphertext size is 65,535 bytes; maximum transport plaintext is 65,519 bytes,
including the entire JSON envelope. Large chat requests must be rejected by the
client before sending; request chunking is reserved for a future version.

HTTP(S) server origin becomes `ws(s)://host[:port]/symlink/v1`. A network path must
support WebSockets; an SSE-only path is insufficient. The chat response's SSE
bytes travel *inside* encrypted WebSocket messages, not as outer HTTP SSE.
TLS is still recommended for public paths. Never retry plaintext or another
handshake pattern after authentication fails.

## Invitation

The QR encodes `engrai://pair#<base64url(JSON)>`. Its JSON fields are:

```json
{
  "version": 1,
  "endpoint": "wss://host.example/symlink/v1",
  "server_key": "<server X25519 public key>",
  "invitation_id": "<32 lowercase hexadecimal characters>",
  "secret": "<random 32-byte pairing PSK>",
  "expires_at": 1800000000.0
}
```

Validate the version, URL, encodings and expiry before use. Treat all invitation
content as sensitive. Never log the URI or include it in analytics. The client
generates an X25519 identity locally and stores its private key in Apple Keychain
with a device-only accessibility policy appropriate to the app. Do not reuse
the pairing PSK as the client private key.

## Initial handshake

Connect to `<endpoint>/pair/<invitation_id>`.

Use `Noise_XXpsk0_25519_ChaChaPoly_SHA256`, with the decoded invitation secret as
the 32-byte PSK. Both sides supply their own static X25519 key and generate fresh
ephemeral keys for every connection. All three handshake payloads are empty.

The prologue is the following ASCII bytes, with LF separators and **no final LF**:

```text
ENGRAI-SymLink/1
pair
<invitation_id>
<server_key as the exact base64url string>
```

1. Client writes Noise message 1; server reads it.
2. Server writes message 2; client reads it and checks the authenticated remote
   static key equals the server key pinned by the invitation.
3. Client writes message 3; server reads it. The server atomically rechecks
   invitation validity, persists that client's public key, and consumes the invite.
4. Server sends an encrypted `ready` record. The client saves the returned peer ID
   alongside the server public key, endpoint and its own private key. Erase the PSK.

An interrupted handshake does not enroll a device. If the server commits enrollment
but the connection drops before `ready` arrives, the administrator can remove that
device and generate a new invitation. Expired, cancelled or concurrently consumed
invitations cannot finish enrollment. Invitations disappear on server restart.

## Reconnect

Connect to `<endpoint>/session/<peer_id>`.

Use `Noise_IK_25519_ChaChaPoly_SHA256` with the client's saved private key and
the pinned server public key as the initiator's remote static key. No pairing
PSK is used. Both handshake payloads are empty; do not send early application data.

Prologue, again with no final LF:

```text
ENGRAI-SymLink/1
session
<peer_id>
<server_key as the exact base64url string>
```

The client sends message 1. The server checks the authenticated remote static key
against the active peer record **before** sending message 2. The client reads
message 2 and then the encrypted `ready` record. Never accept an unexpected server
key, including after a server reinstall. Re-pair explicitly if its identity changes.
Every reconnect uses fresh Noise ephemeral keys and cipher counters.

## Encrypted application records

After the handshake, encrypt/decrypt each complete JSON object with the Noise
transport cipher. Nonces are managed by the Noise implementation; replayed,
altered or out-of-order ciphertext terminates the session. Server welcome:

```json
{"type":"ready","version":1,"peer_id":"<id>","permissions":["models.list","chat.completions"],"max_frame":65535}
```

Client requests use a unique ID of 1–64 ASCII letters, digits, underscores or
hyphens. Wait for `response.end` before sending the next request. At most 1,024
requests are accepted per connection; reconnect before reaching that limit.

```json
{"type":"request","id":"models_1","operation":"models.list"}
{"type":"request","id":"chat_1","operation":"chat.completions","body":{"model":"route-id","messages":[{"role":"user","content":"Hello"}],"stream":true}}
{"type":"request","id":"ping_1","operation":"ping"}
```

Each response consists of these encrypted records:

```json
{"type":"response.start","id":"chat_1","status":200,"content_type":"text/event-stream"}
{"type":"response.chunk","id":"chat_1","data":"<standard base64 of response bytes>"}
{"type":"response.end","id":"chat_1"}
```

Concatenate decoded chunk bytes in order. Chunk boundaries need not match UTF-8
characters, JSON values, or SSE events. Decode incrementally. `models.list` returns
the existing OpenAI-shaped list of routable text models. `chat.completions` uses
the existing gateway's routing, loading, prompt, ledger and inference semantics.
`stream:false` produces a JSON response through the same record sequence.

Unknown operations return 403 inside the encrypted channel. There is no arbitrary
HTTP forwarding, shell access, file access, administrator delegation, or access to
credential management. All paired devices currently receive the same two explicit
permissions. A future protocol can negotiate additional capabilities.

The response is complete only after `response.end`. A disconnect is an incomplete
request, even if some text arrived. Do not automatically retry generation: it may
already have performed work. Closing the WebSocket cancels an active request.
Cancellation cannot undo completed work or ledger changes. No concurrent requests
or in-band cancellation are specified in v1.

## Limits and operation

- 10-second total handshake deadline; 120-second idle timeout; 300-second request
  deadline. An active request is governed by its request deadline, not idle time.
- 32 simultaneous WebSocket connections per gateway process, including handshakes.
- 8 pending invitations and 128 paired devices. Invitations are process-local;
  run the existing single-worker gateway for pairing (no multi-worker load balancing).
- Revocation is checked on every response record and at 250 ms intervals while
  idle or waiting for inference. It cancels the request task and closes the channel.
- Authentication/protocol failures close with WebSocket code 1008 and a generic
  reason. Clients must not interpret a transport close as a successful response.
- The CLI sets a 65,535-byte WebSocket message limit, queue size 4 and disables
  compression. If launching Uvicorn directly, configure the equivalent options.

The server's private identity and public peer records are in `symlink.json` beside
the credential store, or `ENGRAI_SYMLINK_STATE_PATH`. Files are created mode 0600
and replaced atomically under a file lock. Back up this file privately if preserving
pairings across reinstall matters. Corrupt state fails closed; it is never silently
replaced with a new identity. Invitation secrets exist only in process memory and
the administrator's displayed QR. Losing server state requires explicit re-pairing.

## Implementation and interoperability

The crypto adapter uses pinned `noiseprotocol` with `cryptography`; QR generation
uses local `qrcode`. These are replaceable libraries, not networking services.
The server extracts the authenticated remote static key from the library handshake
state; that compatibility boundary is covered by reconnect and wrong-key tests.

See [Noise revision 34](https://noiseprotocol.org/noise.html) for the cryptographic
wire format. [symlink-v1-vectors.json](symlink-v1-vectors.json) supplies deterministic
pairing/reconnection vectors for the native client. **All private and ephemeral
keys in those fixtures are synthetic test values and must never be used in an app.**
They lock down this protocol's byte-level interoperability; they are not an
independent security audit. Test both patterns and rejected server keys before
connecting a native client to real user data.
