# Repo B deploy-key access test

SyncApp now has a narrow, read-only boundary for proving that one protected
deploy-key generation can authenticate to the configured private Repo B. This
is an explicit operator action: it is not run by startup or Retrigger and it
does not enroll, rotate, remove, or select a key for deployment transport.

Before opening SSH, `test_repo_b_deploy_key_access()` uses the existing
token-authenticated GitHub metadata boundary to re-prove the configured
`owner/repository`, privacy, and pinned numeric repository ID. Deploy keys
cannot perform that REST identity check themselves. A missing credential,
changed repository identity, or non-private repository therefore prevents SSH
from starting.

The implementation then inspects the existing protected generation without
creating or repairing it. The private key is opened with no-follow semantics,
verified as a single-link `0600` regular file owned by the App user, and passed
to OpenSSH only as an inherited descriptor. Private bytes, the persistent key
path, and the GitHub token never appear in the command, URL, environment,
result, or error.

Exactly one bounded, noninteractive `git ls-remote --refs` request is made to
`ssh://git@github.com/<owner>/<repository>.git`. Git hooks, credential helpers,
file transport, prompts, forwarding, local SSH configuration, and password or
keyboard-interactive authentication are disabled. The command runs in a
protected `0700` directory with an allowlisted environment and a fixed timeout.
Stdout and stderr are read incrementally; crossing either byte limit terminates
the entire process group, so untrusted output is not buffered without bound.

GitHub's published Ed25519 host key is packaged as `/app/github_known_hosts`.
Its exact bytes, regular-file metadata, and official
`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU` fingerprint are verified
before SSH. Strict host-key checking is mandatory; TOFU and user/global known
hosts are not used. Update this pin only in a separately reviewed change that
re-verifies GitHub's official SSH host-key documentation and REST `/meta`
response.

Successful output must be empty (a valid repository with no refs) or a
canonical, sorted, unique sequence of lowercase SHA-1/SHA-256 object IDs and
`refs/...` names. Symbolic `HEAD`, peeled refs, malformed separators, duplicate
or unsorted refs, noncanonical hashes, missing final newlines, excessive refs,
and oversized output fail closed. The immutable proof contains only the target,
numeric repository ID, deploy-key fingerprint and generation UUID, ref count,
and a SHA-256 digest of the observation. It contains no ref name or object ID.

DNS, route, connection-reset/refused, and timeout failures are transient.
Authentication, repository visibility, host-key, malformed-evidence, missing
generation, and identity failures are deterministic. All errors are sanitized;
raw stderr and repository content are never surfaced.

This proof grants no mutation or deployment authority. Existing token-based Git
transport and every validation, backup, observation, promotion, rollback,
locking, and Retrigger safeguard remain unchanged. GitHub enrollment/removal,
rotation, deploy-key transport, explicit Repo B initialization, runtime wiring,
and administrative UI remain separate reviewed increments under story #388.
