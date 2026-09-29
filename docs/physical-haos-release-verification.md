# Physical Home Assistant OS release verification

This runbook supports the physical release gates in [#251](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/251) and [#212](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/212). The sole product specification remains the initial V2 root README at commit `71d284ce447d79b044e332c9bc01ae801dc91947`.

It is an **evidence-collection procedure**, not implementation authority. Completing this document in source control does not close #251 or #212. Those tasks require observations from a physical Home Assistant OS installation.

## Safety rules

Use a disposable physical Home Assistant OS test installation. Prefer Raspberry Pi 5/aarch64 because the initial V2 specification names Raspberry Pi 5 as the primary target; retain the repository's native amd64 CI evidence as the second declared architecture.

Keep SyncApp protection mode enabled. Do not grant SyncApp additional host, Docker, network-namespace, filesystem, or privileged access merely to make a check pass. Do not change the shipped AppArmor policy to complain mode. Do not disable, pause, remove, or replace the recurring Retrigger mechanism.

Use only synthetic candidate data and a disposable private Repo B. Do **not** exercise candidate Apply, promotion, rollback, backup restore, or intentional live Home Assistant configuration mutation on a production installation.

If observation requires Home Assistant OS debug SSH, use it only on the disposable test host as an external observer. Debug access is not a SyncApp runtime dependency and must not be represented as one.

Never copy repository authentication values, Home Assistant API material, `SUPERVISOR_TOKEN`, private repository contents, or unrelated configuration into the evidence record.

## Evidence header

Create a verification note for the run and record these values before testing:

```text
Date/time (UTC):
Tester:
Physical hardware:
CPU architecture:
Home Assistant OS version:
Supervisor version:
Home Assistant Core version:
SyncApp repository:
SyncApp commit SHA:
SyncApp App version:
Repo B: disposable private repository (name redacted if necessary)
Protection mode: enabled
Initial README authority: 71d284ce447d79b044e332c9bc01ae801dc91947
```

The SyncApp commit must be the exact commit being certified. Do not describe a later branch or unreviewed local build as the tested release.

## 1. Repository discovery and installation — #251

1. Add this repository as a Home Assistant App repository on the disposable HAOS system.
2. Confirm **Home Assistant SyncApp V2** is discoverable.
3. Confirm the displayed App version matches the checked-out `syncapp/config.yaml`.
4. Install that exact build.
5. Before first start, confirm protection mode is still enabled.
6. Start the App and retain the first sanitized lifecycle log events.

**Pass:** the App is discoverable/installable on the physical test system, starts with protection mode enabled, and does not require additional undocumented privileges.

Record:

```text
[#251 discovery/install]
Repository visible:
App install succeeded:
Protection mode enabled:
Unexpected privilege request:
Relevant sanitized log event(s):
PASS/FAIL:
```

## 2. Read-only Home Assistant configuration mount — #251

The shipped App maps `homeassistant_config` to `/homeassistant` as read-only. Verify this from the running physical App container using observation-only host debug tooling.

1. Identify the running SyncApp container without changing its security configuration.
2. Inspect its mount metadata and confirm the `/homeassistant` mount is read-only.
3. Attempt to create a uniquely named disposable probe file under `/homeassistant`.
4. The write must fail because the filesystem is read-only.
5. Confirm the probe file does **not** exist in Home Assistant configuration afterward.

Do not remount the filesystem, change mount flags, or temporarily enable write access.

**Pass:** mount metadata reports read-only and the attempted App-side write is denied without changing live configuration.

Record:

```text
[#251 read-only mount]
/homeassistant mounted:
Read-only flag observed:
Probe write denied:
Probe absent after attempt:
PASS/FAIL:
```

## 3. Persistent App identity across stop/start and reboot — #251

1. Record the sanitized installation identity and boot/run evidence emitted by the App.
2. Stop the App normally.
3. Confirm shutdown is clean and the recurring Retrigger dispatcher is disarmed as part of process shutdown.
4. Start the App again.
5. Confirm the stable installation identity is unchanged and protected state is not silently reinitialized.
6. Reboot the complete Home Assistant OS host.
7. After HAOS and Supervisor are healthy, confirm `boot: auto` started SyncApp automatically.
8. Confirm the same installation identity and protected state remain available after the host reboot.

**Pass:** stop/start and full host reboot preserve installation identity/state; the service returns automatically and clean shutdown does not leave duplicate ownership.

Record:

```text
[#251 persistence]
Identity before stop:
Clean stop observed:
Identity after App restart:
Identity after HAOS reboot:
Automatic boot observed:
Duplicate state/service ownership observed: no
PASS/FAIL:
```

## 4. Retrigger lifecycle after reboot — #251

Use the configured, non-disabled `retrigger_interval_seconds`. Do not shorten it below the supported schema minimum and do not disable the mechanism.

1. With the disposable private Repo B configured and trusted, restart the App.
2. Observe normal startup.
3. Wait at least one configured Retrigger interval.
4. Confirm a bounded Retrigger request is serviced without duplicate concurrent ownership.
5. Reboot HAOS.
6. Confirm the App returns automatically.
7. Wait at least one configured interval and confirm Retrigger servicing resumes.
8. Stop the App normally and confirm no further dispatcher activity occurs after process shutdown.

**Pass:** recurring recovery returns after reboot, remains single-owner, and stops only with App shutdown.

Record aggregate/sanitized evidence only:

```text
[#251 Retrigger]
Configured interval:
First post-start cycle observed:
First post-reboot cycle observed:
Concurrent/duplicate ownership observed: no
Dispatcher quiet after App stop:
PASS/FAIL:
```

## 5. Fail-closed configuration and repository trust — #251

Perform these checks one at a time and restore the valid disposable configuration between cases.

1. Supply an invalid supported-option value and confirm startup fails closed with a sanitized reason.
2. Configure a repository target that is not the previously trusted private Repo B and confirm repository trust fails closed.
3. If testing a public repository case, use only a disposable repository containing no Home Assistant data.
4. Confirm diagnostics do not expose authentication material, repository contents, raw Home Assistant configuration, or nested exception text.

Do not weaken privacy checks or repository identity binding to obtain a successful start.

**Pass:** invalid options and invalid repository trust prevent unsafe operation and diagnostics remain sanitized.

Record:

```text
[#251 fail closed]
Invalid option rejected:
Untrusted/mismatched repository rejected:
Sensitive material exposed: no
PASS/FAIL:
```

## 6. Validator AppArmor enforcement — #212

This section verifies the nested semantic-validator confinement already exercised in native CI, but on the Supervisor-adjusted physical App.

1. Confirm the SyncApp parent AppArmor profile is loaded in **enforce** mode.
2. Trigger validation only with synthetic staged candidate data.
3. While validation is active, confirm the expected nested validator profile is loaded in **enforce** mode.
4. Do not switch either profile to complain mode.
5. If the expected profile is absent, malformed, or not enforcing, treat the run as failed; do not bypass confinement.

**Pass:** both the installed App's effective parent profile and nested validator profile are enforcing on physical HAOS.

Record:

```text
[#212 AppArmor]
Parent profile identity:
Parent profile enforcing:
Nested validator profile identity:
Nested profile enforcing:
Unexpected profile relaxation: no
PASS/FAIL:
```

## 7. Synthetic semantic validation behavior — #212

Use only disposable synthetic candidate data. Do not point this check at live Home Assistant configuration.

Run three cases through the shipped exact-version semantic-validation path:

1. A minimal valid synthetic candidate expected to pass.
2. A syntactically or semantically invalid synthetic candidate expected to fail.
3. A warning-producing synthetic candidate expected to be rejected according to the current fail-closed validation contract.

Record the exact Core version used for the check and the SyncApp commit.

**Pass:** valid synthetic input reaches the expected exact-version checker and passes; invalid and warning-producing candidates are rejected.

```text
[#212 semantic behavior]
Exact Core version:
Valid synthetic candidate: PASS/FAIL
Invalid synthetic candidate rejected: yes/no
Warning-producing synthetic candidate rejected: yes/no
PASS/FAIL:
```

## 8. Validator confinement probes — #212

The validator must remain unable to escape its intended sandbox. Use synthetic marker files whose contents are safe to disclose and keep probes outside production data.

Verify that the nested validator cannot:

- read the live `/homeassistant` tree;
- read SyncApp durable `/data`;
- read unrelated `/tmp` content outside its assigned sandbox;
- open IPv4 or IPv6 network sockets;
- execute alternate binaries outside the approved validator command path.

The purpose is to observe denials, not to weaken the profile until a probe succeeds. A missing expected denial is a release-blocking failure that should become a defect.

```text
[#212 confinement]
Live /homeassistant denied:
Durable /data denied:
Unrelated /tmp denied:
IPv4 socket denied:
IPv6 socket denied:
Alternate executable denied:
PASS/FAIL:
```

## 9. Reboot persistence of confinement — #212

1. Reboot the HAOS host after a successful enforcing-profile check.
2. Confirm SyncApp returns via `boot: auto`.
3. Re-run a synthetic validation.
4. Confirm the effective parent and nested profiles are still enforcing.
5. Confirm validation fails closed rather than running unconfined if the expected nested profile is unavailable.

**Pass:** reboot does not silently relax semantic validation.

```text
[#212 reboot confinement]
App returned automatically:
Parent profile enforcing after reboot:
Nested profile enforcing after reboot:
Fail-closed behavior verified:
PASS/FAIL:
```

## 10. Evidence review and issue updates

Before marking either physical gate complete:

- ensure every acceptance criterion in #251 or #212 has a corresponding observation above;
- attach only sanitized screenshots/log excerpts;
- include exact SyncApp commit and platform versions;
- distinguish native CI evidence from physical HAOS evidence;
- record any failed criterion as a separate defect rather than retrying an unchanged deterministic failure until it appears to pass;
- leave #251 or #212 open if any required observation is missing.

A physical run is successful only when all criteria for the relevant issue pass on the recorded exact build.

## Copyable completion matrix

```text
Initial README: 71d284ce447d79b044e332c9bc01ae801dc91947
Exact SyncApp commit:

#251
[ ] Repository discovery/install
[ ] Physical aarch64 installation
[ ] Protection mode / no extra privileges
[ ] /homeassistant read-only + denied write probe
[ ] /data/syncapp identity/state persistence
[ ] boot:auto + Retrigger returns after reboot
[ ] clean stop disarms dispatcher
[ ] invalid configuration fails closed
[ ] repository trust mismatch fails closed
[ ] platform/build evidence recorded safely

#212
[ ] Physical build installed without weakened protection
[ ] Parent and nested AppArmor profiles enforce
[ ] Valid synthetic candidate accepted
[ ] Invalid synthetic candidate rejected
[ ] Warning-producing synthetic candidate rejected
[ ] /homeassistant denied
[ ] /data denied
[ ] unrelated /tmp denied
[ ] IPv4/IPv6 sockets denied
[ ] alternate binaries denied
[ ] reboot retains confinement / missing profile fails closed
[ ] platform/build/profile evidence recorded safely

Production Home Assistant mutated: no
Retrigger disabled/paused/deleted: no
Secrets/private data included in evidence: no
```

This runbook **does not close** #251 or #212 by existing in the repository. Only a completed, reviewed physical evidence record for the exact tested build can satisfy those release gates.
