# Proposal

## Why

Ordinary service operation detects trusted candidate commits but leaves their entire preflight to the recovery scheduler. A bounded normal preflight processor will produce validation evidence promptly while keeping interruption recovery and deployment mutations behind their existing boundaries.

Tracked by [task #470](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/470), derived from the initial root README candidate deployment and independent recovery requirements.

## What Changes

- Process one due, pending candidate action per ordinary service tick through exact Fetch/Stage, integrity analysis, dependency analysis, risk classification, static validation and isolated exact-version semantic validation.
- Reuse the existing typed executors, persisted checkpoints and failure classifications; preserve the exact candidate SHA and all upstream evidence bindings.
- Stop normal processing at `semantically_validated / prepare_backup`. Leave backup, Apply, restart, observation, promotion and rollback to their existing guarded paths.
- Keep `running`, `retry`, blocked and terminal work outside routine claims. Retrigger retains interrupted-work recovery and retry processing without scheduling changes.
- Preserve candidate observation/Fetch-Stage transport selection and the currently separate token-backed integrity/change-analysis transport.
- Add synthetic tests, fixed diagnostic events and operational documentation for this complete preflight path.

## Capabilities

### New Capabilities

- `routine-candidate-preflight`: Normal service lifecycle, bounded pending-work claims and read-only candidate validation through the backup handoff.

### Modified Capabilities

None. This is the repository's first OpenSpec change; the initial root README remains the sole product specification.

## Impact

Add a small candidate preflight processor and cadence service, integrate them into `ha_syncapp.__main__`, and document/test the behavior. Existing deployment executors, state schema, recovery policy, authentication selectors and App permissions remain intact. No new runtime dependency or live Home Assistant operation is required to implement or verify this increment. Physical HAOS release gates #212 and #251 remain open.
