# Repository setup and configuration sync (0.2.0)

This experimental increment provides an admin control panel, SSH deploy keys,
explicit Repo B initialization, automatic local configuration sync and durable
retries. Candidate deployment and database/runtime/log export remain in
development. Those three generated branches initially contain status manifests.
Remote proposals are never copied into live configuration in this release.

## Install and configure

1. Add https://github.com/Togarriapa/HomeAssistant_SyncAppV2 in Settings → Apps →
   App store → Repositories. Install the app on a test Home Assistant OS system.
   Keep protection mode enabled.
2. Create Repo B as a **private, empty GitHub repository**, without a README,
   license or initial commit. Repo A is public app source and must never be the
   destination for real configuration.
3. In the app Configuration tab, set `repository` to `owner/name`. Create a
   fine-grained GitHub token restricted to Repo B with **Metadata: read-only**
   permission and set `github_metadata_token`. This token verifies privacy,
   archive status and stable repository identity. It is never passed to Git.
   All Git content transfers use SSH.
4. Start the app and open its Web UI. Select **Generate Key**. Copy the displayed
   public key into Repo B → Settings → Deploy keys → Add deploy key. Select
   **Allow write access**. The private Ed25519 key stays in protected app storage.
5. Select **Test Key**, then **Activate Tested Key**. The test creates and removes
   a temporary tag containing synthetic data. It does not upload configuration
   or create a default branch.
6. Select **Initialize Repo**. The app verifies Home Assistant health, repository
   privacy and key access, captures configuration, then atomically creates
   `main`, `candidate`, `database`, `runtime` and `logs`.
   **No automatic sync is enqueued or attempted before initialization succeeds.**

A non-empty or conflicting repository is refused. If initialization is interrupted,
the same operation reconciles its recorded commits with GitHub and resumes its
saved snapshot, preserving later local edits for a subsequent sync. Inspect
Operations for its outcome: a completed setup action may have queued an operation
that is still in progress.

## Refresh a deploy key

Select **Refresh Key**, register the pending public key in GitHub with write access,
then **Test Key** and **Activate Tested Key**. Tests expire after ten minutes and
are bound to the repository and key. Failed tests leave the active key unchanged.
Repeated Generate/Refresh actions reuse an existing pending key.

After a successful sync with the replacement, remove the previous deploy key from
GitHub; its fingerprint is displayed. The app retains only the active, pending and
immediately previous key files locally. Removing local files does not revoke
GitHub authorization. Key activation is refused during unresolved deployment or
recovery phases; interrupted Git pushes can safely resume with a replacement.

SSH strictly verifies GitHub's published Ed25519 host key. A changed host key fails
closed and requires an app update after checking
[GitHub's published fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).
The app never learns a key from its first network connection.

## Options

| Option | Default | Accepted values |
| --- | --- | --- |
| `repository` | empty | GitHub `owner/name`; empty permits setup only |
| `github_metadata_token` | empty | Fine-grained token restricted to Repo B metadata |
| `sync_interval_seconds` | 60 | Integer 30–3600 |
| `retrigger_interval_seconds` | 3600 | Integer 60–86400 |
| `log_level` | info | info, warning, error |
| `status_interval_seconds` | 300 | Integer 30–3600; lifecycle log interval |

Save options and restart after changes. Unknown keys, duplicate JSON keys,
invalid types and unsafe repository names prevent startup. Repository identity
becomes bound during verification; changing the option cannot silently redirect
private configuration. Migration to a different Repo B is not implemented.

## Synchronization and recovery

Snapshots preserve raw bytes, including hidden .storage data, CRLF, binaries and
real secrets. Git filters, hooks and live working-tree checkouts are not used.
Root home-assistant_v2.db* and home-assistant.log*, .git, Python bytecode and cache
directories are excluded. Limits are 128 MiB total and 10,000 regular files.
Symlinks and special files fail closed. Git does not reproduce filesystem metadata.

A local change must appear unchanged in two successive polls before pushing.
Home Assistant must be healthy. Main updates use a compare-and-swap lease and
must descend from the recorded main. An unexpected remote main change stops the
operation and preserves both sides. An unprocessed candidate also stops local
sync in this increment; candidate deployment is the next delivery.

Operations have unique durable IDs, phases, attempts and planned commits.
Transient failures retry after 30, 60, 120 seconds and so on, capped at one hour,
with at most eight attempts. Deterministic failures remain blocked until an
explicit Retry action. The configurable hourly Retrigger Work scan records
pending/blocked counts; normal due retries need not wait an hour. A restart
recovers interrupted jobs under the lifetime process lock. Executions, retries,
checkpoints, skips and failures are recorded without raw exception text.

Successful staging snapshots are removed after completion. Interrupted snapshots
stay protected until reconciled. Events retain up to 30 days/100,000 entries;
completed job history retains up to 30 days/10,000 rows. Log export and Git object
cache maintenance are not yet part of this increment.

## Access, persistence and troubleshooting

Configuration is mounted **read-only** in 0.2.0. Supervisor/Core APIs provide health
checks. There is no host networking, Docker socket, hardware access or exposed host
port. The admin Web UI accepts only Supervisor ingress (172.30.32.2), protects
mutations with a per-process CSRF token and serializes every action on one worker.

The app owns /data/syncapp with directory mode 0700 and sensitive files mode 0600.
Credentials and internal state never come from Repo B. Cold app backups stop the
service while copying its state. Schema 1 migrates transactionally to schema 2
without losing installation identity or interrupted-run information. Downgrading
to 0.1.0 with schema 2 is refused; restore its matching app backup.

| Failure | Action |
| --- | --- |
| repository_setup_incomplete | Set repository and metadata token, then restart. |
| deploy_key_not_tested | Register and test the pending key, then activate within ten minutes. |
| git_operation_failed | Check connectivity, key write access and repository rules; retry after correcting the cause. |
| repository_not_private / repository_identity_changed | Verify Repo B privacy and identity; content transfers remain blocked. |
| repository_not_empty | Use an empty private repository; initialization never overwrites existing refs. |
| home_assistant_unhealthy | Resolve Home Assistant/Supervisor health problems, then retry. |
| remote_main_changed | Reconcile the unexpected writer; configuration is never blindly overwritten. |
| candidate_requires_processing | Candidate deployment is not available in this increment. |
| snapshot_corrupt / state_unavailable | Stop the app, preserve its data and restore a compatible backup. |

The persistent instance.lock file is normal; the kernel releases its lock on exit
or reboot. **Never delete or replace it while an instance may be running.**
Container tests cover offline setup, real Git objects, key generation, ingress
denial and lifecycle recovery. They do not certify actual GitHub key registration
or a physical Home Assistant OS installation.
