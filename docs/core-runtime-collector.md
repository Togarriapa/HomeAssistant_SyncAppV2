# Read-only Home Assistant Core runtime collector

This increment is derived only from the initial V2 `README.md` at root commit
`71d284ce447d79b044e332c9bc01ae801dc91947`, specifically its requirement for a generated,
AI-readable `runtime` branch containing current Home Assistant configuration/state/service
information. Home Assistant API mechanics are taken from current official Home Assistant developer
documentation rather than from another SyncApp project.

## Permission boundary

The app manifest enables only `homeassistant_api: true` in addition to the existing read-only
`homeassistant_config` mount. It does not enable `hassio_api`, host networking, Docker access,
privileged mode, device access, ingress, or any other mutation-oriented capability.

Home Assistant documents the App/Core proxy as `http://supervisor/core/api/` and requires the
`SUPERVISOR_TOKEN` bearer credential when `homeassistant_api: true` is enabled. The collector does
not accept an arbitrary production base URL. Its default transport connects only to the internal
`supervisor` host and only to these paths:

- `GET /core/api/config`
- `GET /core/api/states`
- `GET /core/api/services`

The credential is supplied explicitly by the caller or read from `SUPERVISOR_TOKEN`. It is never
included in error messages or runtime inventory output.

## Fail-closed response handling

Every response is bounded by both a request timeout and a maximum byte count. The collector
requires HTTP 200, an `application/json` media type, UTF-8 JSON, finite JSON values, and the
documented top-level shape for each endpoint. Transport failures are converted to sanitized
`CoreRuntimeError` messages.

The complete `/api/config` object is retained in the runtime manifest as `core_config`; its
`version`, state count, and service-domain count are also exposed as summary fields. `/api/states`
and `/api/services` populate the existing `homeassistant/states.json` and
`homeassistant/services.json` datasets when passed through the deterministic runtime artifact
builder.

## Deliberate partial coverage

REST state objects are not Home Assistant entity-registry records. This slice therefore does not
pretend that `/api/states` provides full entities, devices, integrations, areas, floors, or labels.
Those datasets remain empty until later increments implement documented registry/inventory
collection mechanisms.

This slice also does not:

- call Home Assistant write/service endpoints;
- use the Home Assistant WebSocket API;
- call Supervisor mutation APIs;
- publish the runtime artifact on a schedule;
- process `candidate` changes;
- modify the live Home Assistant configuration;
- create backups, restart Home Assistant, observe deployments, or roll back changes.

The existing guarded runtime publication boundary remains responsible for publishing generated
runtime evidence to Repo B.
