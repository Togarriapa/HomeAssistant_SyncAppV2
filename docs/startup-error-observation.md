# Post-restart startup-error observation

SyncApp evaluates startup errors only after the exact deployment has completed
its Core health window, Supervisor health proof, and integration-initialization
proof. The observation opens one bounded authenticated Core WebSocket session
and sends only the read-only `system_log/list` command.

Home Assistant's system-log integration captures warnings and errors in a
bounded deduplicated store and exposes them through an admin-only WebSocket
command. Its response contract is pinned for review to
[Home Assistant Core commit `949a484`](https://github.com/home-assistant/core/blob/949a484720ac3ebd2f474ce7e408a800c6c4ebdc/homeassistant/components/system_log/__init__.py#L1253-L1350).

## Classification and time authority

The interval starts at the exact acknowledged Core restart time. Records whose
latest occurrence predates that instant are ignored. New `WARNING` entries are
counted for diagnostics, while new `ERROR` and `CRITICAL` entries produce the
durable `significant_errors` outcome. A failed outcome is persisted and replayed
without another request, preventing an unchanged deterministic failure from
becoming an automatic retry loop.

Every returned entry must match the pinned Core shape. Invalid severity, time,
count, source, message, duplicate, future, oversized, or ambiguous evidence
fails closed without creating authority.

## Durable authority and privacy

Schema v17 stores only the deployment identifier, exact prerequisite digests,
interval timestamps, inspected-entry count, warning count, significant-error
count, and an integrity digest. Logger names, messages, exceptions, source
paths, credentials, response bodies, and transport errors are never persisted.

The transaction re-proves the exact integration and restart bindings before
writing. Both clear and significant-error results replay without credentials or
network access. This observer is read-only: it cannot clear logs, change logger
configuration, restart Core, mutate Home Assistant state, or write Git refs.
