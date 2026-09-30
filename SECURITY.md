# Security policy

## Reporting

Use GitHub private vulnerability reporting for this repository when available.
Do not put API keys, passwords, credential-store contents, tunnel credentials,
or private model paths in a public issue.

## Deployment boundary

Only ENGRAI should be reachable through a LAN, WireGuard, or reverse-proxy
listener. Every engine must remain on loopback or private IPC. Treat a
deployment that exposes an engine worker port, native API, or bundled UI as
unsupported and unsafe.

Protect the XDG state directory, WireGuard/reverse-proxy configuration, and
runtime receipts as operator state. A checksum mismatch reported by runtime
status or preflight should be investigated before starting a new worker.

The Hugging Face pull API is admin-only. Its destination is fixed by
`ENGRAI_MODEL_DOWNLOAD_DIR`; callers cannot supply a filesystem path, and an
include filter is mandatory to prevent unbounded repository pulls. Inject
`HF_TOKEN` as a scoped read-only process/container secret. ENGRAI relies on
the Hub library's standard credential lookup and never returns the token to
the browser or writes it to download history.
