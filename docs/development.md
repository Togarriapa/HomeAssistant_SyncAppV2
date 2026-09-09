# Development

The root README and P0 issue #10 define the product contract. The current executable foundation was delivered through epic #1/story #2/task #3. The active MVP is issue #7; complete-tree synchronization is issue #8 and Repo B setup is issue #9.

## Non-negotiable invariants

Before changing synchronization or deployment code, preserve these rules:

- Repo B represents the complete configured Home Assistant tree; logs are the sole intentional main-tree routing exception.
- Do not reintroduce blanket exclusions for `.storage`, secrets, credentials, certificates/keys, Recorder database/WAL, generated/runtime/cache or binary files solely because of file class.
- Repo A, fixtures and diagnostic logs must remain free of real Home Assistant secrets and private Repo B credentials. Use synthetic test material only.
- SyncApp deploy keys, metadata credentials and internal journals remain under protected app storage, outside the synchronized Home Assistant tree.
- GitHub → Home Assistant is always **Detect → Fetch → Stage → Validate → Backup → Apply → Verify → Rollback if necessary**.
- Never run `git pull`, checkout, merge, hooks or filters inside the live Home Assistant configuration tree.
- A remote candidate may be visible but still fail closed for automatic mutation if safe validation/apply semantics are unavailable.
- Do not merge risky or failing changes.

## Local checks

Use Linux and Python 3.12 for the current foundation. Linux file locking is part of the runtime contract. The service currently uses only the Python standard library; development dependencies are pinned separately and are not installed in the app image.

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

Press Ctrl+C to record a clean stop. The app logs only selected, sanitized events. As the project gains access to the Home Assistant tree, do not add raw configuration, credential contents, private keys or arbitrary exception text to diagnostic events.

Build and smoke-test a container on an amd64 Linux host with Docker:

```sh
docker build --build-arg BUILD_ARCH=amd64 -t syncapp:test syncapp
python3 scripts/container_smoke.py
```

Use `BUILD_ARCH=aarch64` on an arm64 Linux host. CI builds natively on both architectures, starts the image without networking, stops it and restarts it with the same `/data` to verify identity persistence. These are container tests, not Supervisor or physical Raspberry Pi certification.

## TDD and failure-path requirements

Add measurable acceptance criteria to a story before implementing behavior. Start with a failing behavior test, implement the minimum safe behavior, then refactor while keeping tests green.

Critical synchronization/deployment changes require tests for failure boundaries, not only happy paths. Examples include:

- concurrent mutation during snapshot;
- hidden/sensitive/binary/database/runtime content preservation;
- sole log-routing exception;
- unsafe symlinks/path traversal/hardlink ambiguity;
- repository identity/privacy mismatch;
- lost Git push acknowledgement;
- local/remote divergence;
- staged-byte mutation after validation;
- backup failure or ambiguous backup completion;
- process crash at each durable transaction phase;
- apply failure;
- verification failure;
- rollback failure and rollback-health verification;
- deterministic rejected-candidate retry blocking.

Regression tests should explicitly prevent stale product assumptions from returning. A test that protects a safety policy must distinguish between **visibility policy** and **mutation policy**: complete private Repo B visibility is required, while automatic live mutation remains fail-closed where evidence is insufficient.

## Pull-request gate

A synchronization/deployment PR is not ready to merge until:

1. linked acceptance criteria exist;
2. relevant unit/integration/failure-injection tests pass;
3. formatting/lint/type/security checks pass;
4. native amd64/aarch64 container CI passes where applicable;
5. documentation matches behavior;
6. no unresolved review finding invalidates the safety transaction;
7. the implementation does not bypass staging, validation, backup or rollback.

## Packaging references

- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Local app tutorial](https://developers.home-assistant.io/docs/apps/tutorial/)
- [App repository format](https://developers.home-assistant.io/docs/apps/repository/)
- [Official Python image definitions](https://github.com/docker-library/official-images/blob/master/library/python)

The current Dockerfile sets its base explicitly and uses `BUILD_ARCH`/`BUILD_VERSION` labels. There is no legacy `build.yaml`. Docker init is enabled because this Python image has no S6 init. Dependabot tracks base-image, tool and Actions updates. No image is published by the current CI workflow.
