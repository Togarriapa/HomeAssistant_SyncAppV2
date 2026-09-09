# Development

The root README is the target specification. The first implementation is tracked
by [epic #1](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/1),
[story #2](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/2) and
[task #3](https://github.com/Togarriapa/HomeAssistant_SyncAppV2/issues/3).

## Local checks

Use Linux and Python 3.12. Linux file locking is part of the runtime contract.
The service uses the Python standard library plus Git and OpenSSH command-line
programs installed in the image. Development dependencies are pinned separately.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy
.venv/bin/python -m bandit -q -r syncapp/src
```

Run a local service with a temporary data directory:

```sh
mkdir -p local-data
printf '{}\n' > local-data/options.json
PYTHONPATH=syncapp/src .venv/bin/python -m ha_syncapp --data-dir local-data
```

Press Ctrl+C to record a clean stop. The app logs only fixed events and validated
identifiers. Do not add raw configuration, credentials, paths or exception text
to diagnostic events. This source repository is public; use synthetic fixtures.

Build and smoke-test a container on an amd64 Linux host with Docker:

```sh
docker build --build-arg BUILD_ARCH=amd64 -t syncapp:test syncapp
python3 scripts/container_smoke.py
```

Use `BUILD_ARCH=aarch64` on an arm64 Linux host. CI builds natively on both
architectures, verifies identity persistence across restarts, and exercises offline
setup, Ed25519 generation, raw Git transfers and non-ingress denial in the actual
image. These are container tests, not physical Home Assistant OS certification.
Never add real credentials or configuration fixtures to this public repository.

## Synchronization safety boundaries

Git operations are progressively introduced only inside isolated mutable workspaces,
never in the live Home Assistant configuration tree. Accepted source snapshots are
integrity evidence; mutable Git workspaces must reproduce that snapshot identity
before and after a machine commit is accepted.

Repo B network operations have a separate trust boundary. Before branch state is
used for synchronization decisions, SyncApp re-verifies that the configured target
is still the expected private repository with the previously bound repository ID.
Branch-head inspection is read-only, validates the exact requested branch and
commit identity, and deliberately performs no clone, fetch, pull, push, repository
mutation or Home Assistant write.

Before a future push is even eligible, publication preflight compares three pieces
of evidence: the verified local commit, the last successful synchronization
baseline and the trusted current remote branch head. Only an unchanged remote
baseline with a different verified local commit is `safe_to_publish`. Equal state
is a no-op; a remote head already equal to the local commit is recoverable as an
interrupted already-published operation; unrelated remote movement is divergence;
and an existing remote branch without a baseline is blocked. These classifications
are side-effect free and cannot themselves publish anything.

Remote `candidate` content remains deployment input rather than trusted live state.
Nothing in the local synchronization path bypasses the root README's validation,
backup, deployment, observation and rollback requirements.

## TDD and integration

Add measurable acceptance criteria to a story before implementing it. Start with
a failing behavior test, implement it, then refactor. Failure-path tests matter
for configuration/state/locking; subprocess tests exercise actual signals and
kernel lock release. A pull request needs green CI and review before merge.
The foundation tests were first run before their implementation existed; the
subprocess tests also exposed a shutdown race in an early wait-loop implementation.

## Packaging references

- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Local app tutorial](https://developers.home-assistant.io/docs/apps/tutorial/)
- [App repository format](https://developers.home-assistant.io/docs/apps/repository/)
- [Official Python image definitions](https://github.com/docker-library/official-images/blob/master/library/python)

The Dockerfile sets its base explicitly and uses `BUILD_ARCH`/`BUILD_VERSION`
labels. There is no legacy `build.yaml`. Docker init is enabled because this
Python image has no S6 init. The Python base is version-tagged; Dependabot tracks
base-image, tool and Actions updates. No image is published by this CI workflow.
