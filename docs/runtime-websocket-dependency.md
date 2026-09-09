# Home Assistant Core WebSocket dependency boundary

This packaging increment is derived from the initial V2 `README.md` at root commit
`71d284ce447d79b044e332c9bc01ae801dc91947`, which requires generated AI-readable runtime
inventory including entities and devices.

Home Assistant's current official App communication documentation exposes the Core WebSocket API
through `ws://supervisor/core/websocket` when `homeassistant_api: true` is enabled. The same
`SUPERVISOR_TOKEN` used for the Core API is used for WebSocket authentication. No broader
`hassio_api` capability is required for this Core proxy.

V2 uses the `websockets` Python package as the protocol implementation instead of maintaining a
custom RFC 6455 framing/handshake implementation. Version 17.1 is pinned to the published universal
Python wheel SHA-256 in `syncapp/requirements.txt`. The app image installs that file with
`--require-hashes`; the development environment includes the same runtime requirements.

This milestone grants no new Home Assistant or Supervisor permission and sends no WebSocket traffic.
It does not authorize WebSocket mutation commands. A later increment must independently whitelist
specific documented read-only command types, validate the authentication/result protocol, bound
message sizes/timeouts, sanitize failures, and test the collector before runtime publication uses
those datasets.
