# Guarded Recorder database publication

The initial V2 README assigns Recorder history to Repo B's dedicated `database`
branch. `synchronize_database_snapshot()` publishes one explicitly supplied
Recorder database through the same trusted, isolated and non-force publication
boundaries used elsewhere in V2.

The cycle is deliberately layered:

1. create a consistent SQLite online backup in isolated database staging;
2. re-stage only that stable backup through the generic manifest/integrity snapshot;
3. create an isolated Git workspace on the exact `database` branch;
4. inspect the pinned private Repo B and its exact remote `database` head;
5. compare that head with the durable `database` synchronization baseline;
6. block on missing baseline, remote disappearance after a baseline, or divergence;
7. otherwise anchor the trusted remote history, create a snapshot commit only when
   bytes changed, and use the existing non-force publication workflow;
8. accept success only after the remote result is re-read, verified and recorded as
   the new durable branch baseline.

A first `database` publication is permitted only when both the trusted remote branch
and local branch baseline are absent. An existing remote branch without local
baseline evidence is never adopted automatically. Existing history advances only
from the exact recorded baseline, so the workflow does not normalize or overwrite
unknown remote history.

SQLite staging, generic snapshot staging and Git workspaces are removed on success
and failure. Git is never initialized in the Home Assistant configuration tree.
The caller must provide the Recorder database path explicitly; V2 does not guess
custom Recorder locations.

This increment does not implement database scheduling, retention pruning, restore,
custom path discovery, remote database mutation, or the `candidate` deployment
workflow. Those remain separately guarded work.
