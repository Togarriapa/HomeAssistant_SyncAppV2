"""Deterministic fingerprints for read-only Home Assistant runtime evidence."""

from __future__ import annotations

import hashlib
import json

from .runtime_inventory import RuntimeInventoryInput


class RuntimeEvidenceError(RuntimeError):
    """Runtime evidence cannot be represented deterministically."""


def fingerprint_runtime(runtime: RuntimeInventoryInput) -> str:
    """Return a canonical SHA-256 binding for one runtime inventory input."""
    if type(runtime) is not RuntimeInventoryInput:
        raise RuntimeEvidenceError("runtime evidence input is invalid")
    payload = {
        "analysis": runtime.analysis,
        "database": runtime.database,
        "homeassistant": runtime.homeassistant,
        "logs": runtime.logs,
        "manifest": runtime.manifest,
    }
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeEvidenceError("runtime evidence is not canonical JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()
