# Candidate static configuration validation

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, especially the controlled `candidate` deployment pipeline where configuration/Home Assistant validation follows dependency analysis and risk classification and precedes backup and Apply.

## Purpose

`ha_syncapp.candidate_validation.validate_candidate_configuration()` performs a read-only static/configuration-structure pass over the exact isolated candidate Stage. It consumes Candidate Integrity, Dependency/Impact, Risk and runtime evidence and requires their repository identity, baseline SHA, candidate SHA, Stage manifest digest and runtime fingerprint to agree.

Stage is reverified before and after the pass, and every candidate-side file that is read is rebound to its staged size and SHA-256. Candidate content is never copied into the live Home Assistant configuration by this gate.

## Static coverage

The gate currently provides deterministic bounded parsing for:

- YAML/YML using PyYAML `SafeLoader`, with duplicate-key detection. Only Home Assistant's scalar indirection tags `!include`, `!include_dir_list`, `!include_dir_named`, `!include_dir_merge_list`, `!include_dir_merge_named` and `!secret` are added to the safe loader; arbitrary Python/object tags remain rejected.
- JSON, including `.storage/*` candidate files, with duplicate-key and non-finite-number rejection.
- unresolved Git conflict markers in UTF-8 text.

Multiple YAML documents are rejected as ambiguous for this candidate validation layer. Deleted paths are explicit. Binary/non-UTF-8, oversize, and unsupported text formats are emitted as `unvalidated_paths`; they are never silently treated as having passed semantic validation.

`syntax_valid` means only that this static gate found no deterministic syntax/structure failure. It is deliberately paired with `semantic_home_assistant_validation_required=True` and must never be interpreted as deployment authority.

## Why the running Core config-check endpoint is not sufficient

The Home Assistant App can communicate with running Core through the documented Supervisor proxy because `homeassistant_api: true` is enabled. The normal Core configuration-check API validates the running installation's configuration. It does not prove that an arbitrary isolated candidate Stage was the input. Therefore SyncApp must not call the running config-check endpoint and then claim the staged candidate was validated.

The separate semantic gate below checks an isolated copy of the same integrity-bound Stage.

## Safety boundary

This gate does not execute automations, scripts, templates, custom components, or candidate Python code. It performs no Home Assistant writes, service calls, reloads, restarts, backups, Apply, observation, promotion, rejection, or rollback. Failures return path/reason identifiers only; candidate file contents and secrets are not copied into exception messages.

## Exact-version semantic gate

`candidate_semantics.validate_candidate_semantics()` requires a successful complete static
result, the identical upstream evidence, and a Core version bound to the runtime snapshot.
The image bundles the official Core **2026.9.1** runtime. Other running versions remain
blocked until an image with their exact validator is built and verified; there is no
`stable` fallback or runtime package installation. App dependencies use a separate virtual
environment, preserving the Core image's dependency set.

The child invokes the documented `python -m homeassistant --script check_config --config`
entry point with **`--fail-on-warnings`**. Integration schema errors can be warnings, so
warnings also reject the candidate. The checked directory is a disposable private copy;
every original input file is compared afterward, and the original Stage and all evidence
are reverified. Core may create disposable bookkeeping files, but changing an existing
candidate input rejects the result.

Successful evidence binds repository identity, baseline and candidate commits, Stage and
runtime digests, risk and exact Core version. It is a trusted in-process result, not a
signed certificate or an artifact to accept from Repo B. It does not by itself authorize
backup, Apply, reload, restart or promotion. Those transaction stages remain separate.

## Validator isolation and supported scope

The standalone helper starts in isolated Python mode without inherited app credentials.
Before importing Core it drops root identity, sets resource limits, requires Landlock ABI
3 or newer for filesystem restrictions, and installs a libseccomp filter. The filesystem
allowlist includes read-only interpreter resources, fixed Core container-identity marker
files, and the disposable candidate copy; it excludes live configuration and app state.
Network access is denied. New executable launches are denied by Landlock except for the
exact bundled `/usr/local/bin/python3` interpreter that Core may relaunch for dependency-
site discovery and its architecture-specific musl ELF loader. Both exceptions are exact
files, not executable directories; no shell, utility, candidate executable, or alternate
interpreter receives execute permission. Private asyncio wakeup sockets and threads remain
available to Core.

Limits are 180 seconds wall time, 90 seconds CPU, 2 GiB address space, 16 MiB per output
file, 512 file descriptors and 256 processes/threads per validator user. Input is limited
to 128 MiB / 10,000 files and 4 MiB per YAML document source. Raw Core stdout/stderr are
discarded; only fixed result codes leave the child. A missing, oversized, malformed or
incorrectly bound receipt fails closed, as do checker failures or unavailable sandboxing.

This initial path rejects candidate `custom_components/` and `deps/` directories and
absolute/traversing YAML includes, including in unchanged files. It runs Core's schema
validation, not a live HA instance. It cannot prove device behavior, integration runtime
health, or correctness of arbitrary custom Python. Unsupported candidates stay blocked.

No Docker socket, elevated Supervisor role, protection change or host privilege is added.
Native amd64 and aarch64 container CI runs real valid and semantically invalid fixtures,
the warning rejection case, and filesystem/network/exec/identity isolation probes. The
device must also support the required kernel features; no physical HA OS installation
has been validated by these CI checks.

Implementation references: [official Core check_config CLI](https://www.home-assistant.io/docs/tools/check_config/),
[Core 2026.9.1 checker](https://github.com/home-assistant/core/blob/2026.9.1/homeassistant/scripts/check_config.py),
[Landlock filesystem restrictions](https://docs.kernel.org/userspace-api/landlock.html),
and [seccomp filters](https://docs.kernel.org/userspace-api/seccomp_filter.html).
