# Candidate dependency evidence

This increment is derived only from the initial V2 `README.md` at root commit `71d284ce447d79b044e332c9bc01ae801dc91947`, especially the **Remote → Home Assistant Deployment** and **Dependency and Topology Model** sections.

`ha_syncapp.candidate_dependencies.analyze_candidate_dependencies()` is a read-only gate after Candidate Integrity Validation. It accepts the immutable integrity evidence, the exact isolated Stage evidence, and already-collected Home Assistant runtime inventory. It never executes candidate code or templates and performs no Home Assistant writes, service calls, reloads or restarts.

## Binding and reverification

The analysis requires the target repository identity, candidate SHA and Stage manifest digest in Candidate Integrity evidence to match the Stage evidence exactly. Stage is reverified before and after analysis. Each analyzed candidate-side changed file is read from the isolated Stage using the Stage file-safety primitive and its bytes are rebound to the manifest size and SHA-256 before parsing.

Paths present in Candidate Integrity but absent from the candidate Stage are represented as deletions. Unchanged Stage files are not analyzed by this candidate-change gate.

## Conservative static coverage

The current analyzer intentionally provides conservative lexical evidence rather than claiming to interpret the complete Home Assistant configuration language. Literal `domain.object` references found in bounded UTF-8 changed text are classified against entity IDs in the collected runtime registry. Matches are deterministic and marked by the API contract as static/best-effort dependency evidence.

Jinja/template and include/secret indirection markers are surfaced through `dynamic_reference`/`dynamic_paths`. The analyzer never evaluates those expressions. Binary/non-UTF-8 and oversize files are retained as explicit `unanalyzed_paths`; they are never treated as dependency-free merely because static analysis could not inspect them.

This means downstream Risk Classification must treat dynamic or unanalyzed coverage conservatively. This evidence cannot authorize deployment by itself.

## Security boundary

Candidate contents are not copied into exception messages or logs. Analysis occurs only against isolated Stage bytes and returns reference identifiers needed for dependency reasoning. The live Home Assistant configuration is not used as a Git working tree and is not modified by this gate.

Future increments under #133 can add richer syntax-aware automation/script/scene relationships, but they must preserve these integrity bindings, fail-closed coverage reporting and non-execution guarantees.
