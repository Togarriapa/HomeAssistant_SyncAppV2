# Read-only Home Assistant Core runtime collector

This increment is derived only from the initial V2 `README.md` at root commit
`71d284ce447d79b044e332c9bc01ae801dc91947`, specifically its requirement for a generated,
AI-readable `runtime` branch containing current Home Assistant configuration/state/service and
registry information. Home Assistant API mechanics are taken from current official Home Assistant
developer documentation and current Home Assistant Core source rather than from another SyncApp
project.

## Permission boundary

The app manifest enables only `homeassistant_api: true` in addition to the existing read-only
`homeassistant_config` mount. It does not enable `hassio_api`, host networking, Docker access,
privileged mode, device access, ingress, or any other mutation-oriented capability.

The REST collector connects only to the internal `supervisor` host and only to:

- `GET /core/api/config`
- `GET /core/api/states`
- `GET /core/api/services`

The WebSocket registry collector connects only to `ws://supervisor/core/websocket`, performs the
documented authentication exchange using `SUPERVISOR_TOKEN`, and then permits exactly these
one-shot read-only commands:

- `config/entity_registry/list`
- `config/device_registry/list`
- `config/area_registry/list`
- `config/floor_registry/list`
- `config/label_registry/list`
- `config_entries/get`

No subscription or mutation command is part of this boundary. Request identifiers are assigned
monotonically and every result must match the exact request identifier and report `success: true`.
The credential is supplied explicitly by the caller or read from `SUPERVISOR_TOKEN`; it is never
included in runtime inventory output or propagated through nested exception text.

## Fail-closed response handling

REST responses are bounded by request timeout and maximum byte count and require HTTP 200,
`application/json`, UTF-8 JSON, finite JSON values, and the expected top-level shape.

WebSocket messages are likewise bounded by timeout and maximum byte count. Authentication state,
message type, request ID, success flag, JSON shape, stable identity fields and duplicate identities
are checked before data is accepted. Records are retained field-for-field as JSON-compatible
objects and sorted by stable identity before they enter deterministic runtime artifact staging.
Transport and protocol failures are converted to sanitized errors.

The REST `/api/config` object is retained in the runtime manifest as `core_config`; its `version`,
state count, and service-domain count are exposed as summary fields. REST states and services fill
`homeassistant/states.json` and `homeassistant/services.json`. The WebSocket collector fills
`homeassistant/entities.json`, `homeassistant/devices.json`, `homeassistant/areas.json`,
`homeassistant/floors.json`, `homeassistant/labels.json`, and `homeassistant/integrations.json`,
with summary counts in its returned manifest fragment.

`integrations.json` currently means configured Home Assistant config-entry instances returned by
`config_entries/get`. Records are ordered by `entry_id` and preserve the API's domain, title,
source, state, capability/preference and other JSON-compatible fields. The manifest therefore calls
its summary `integration_config_entry_count`; this must not be presented as a complete count of
YAML-only or otherwise config-entry-less loaded integration domains.

## Deliberate partial coverage

This is still only part of the README-defined runtime inventory. Complete loaded-integration
coverage, Supervisor data, hardware data, topology/dependency analysis and deployment observation
require separate, independently justified increments. The current collectors must not invent those
datasets or infer them from incomplete state data.

This slice does not:

- call Home Assistant write/service endpoints;
- send any WebSocket mutation command;
- subscribe to long-lived WebSocket event streams;
- call Supervisor mutation APIs;
- process `candidate` changes;
- modify the live Home Assistant configuration;
- create backups, restart Home Assistant, observe deployments, or roll back changes.

The existing guarded runtime publication boundary remains responsible for publishing generated
runtime evidence to Repo B.
