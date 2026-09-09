# Changelog

## 0.2.0

- Add SSH deploy key generation, testing, activation and safe refresh in an admin ingress panel.
- Require recoverable initialization of an empty private Repo B before automatic synchronization.
- Preserve raw configuration bytes and hidden state using Git objects and leased ref updates.
- Verify repository privacy and identity with a separate read-only metadata token.
- Add persistent operation phases, bounded retries, hourly recovery scans and audit events.
- Migrate foundation state while preserving installation identity and interrupted-run information.
- Add rotation, failure injection, local Git integration, ingress and migration tests.

Candidate deployment and generated database/runtime/log exports are not yet implemented.

## 0.1.0

- Add experimental Home Assistant app packaging for aarch64 and amd64.
- Validate options before creating internal state.
- Preserve installation identity and detect interrupted service runs.
- Prevent concurrent processes using a lifetime lock.
- Add structured lifecycle logs, automated tests and container CI.

Synchronization and deployment were planned capabilities in the initial foundation.
