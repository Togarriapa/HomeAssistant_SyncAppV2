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

A later milestone must establish a version-matched Home Assistant semantic validation mechanism that can prove it consumed the exact same integrity-bound candidate bytes without first writing those bytes into the live Home Assistant configuration.

## Safety boundary

This gate does not execute automations, scripts, templates, custom components, or candidate Python code. It performs no Home Assistant writes, service calls, reloads, restarts, backups, Apply, observation, promotion, rejection, or rollback. Failures return path/reason identifiers only; candidate file contents and secrets are not copied into exception messages.
