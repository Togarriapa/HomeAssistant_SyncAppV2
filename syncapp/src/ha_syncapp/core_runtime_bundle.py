"""Fail-closed composition of read-only Home Assistant Core runtime datasets."""

from __future__ import annotations

from collections.abc import Mapping

from .core_runtime import CoreRuntimeError, collect_core_runtime_inventory
from .core_websocket_runtime import (
    CoreWebSocketRuntimeError,
    collect_core_websocket_inventory,
)
from .runtime_inventory import RuntimeInventoryInput


class CoreRuntimeBundleError(RuntimeError):
    """The bounded Home Assistant Core runtime bundle could not be built safely."""


def collect_core_runtime_bundle(*, token: str | None = None) -> RuntimeInventoryInput:
    """Collect REST and registry datasets and merge them without silent overwrite."""
    try:
        rest = collect_core_runtime_inventory(token=token)
        registries = collect_core_websocket_inventory(token=token)
    except (CoreRuntimeError, CoreWebSocketRuntimeError):
        raise CoreRuntimeBundleError("Home Assistant Core runtime collection failed") from None
    return merge_runtime_inventory_inputs(rest, registries)


def merge_runtime_inventory_inputs(
    first: RuntimeInventoryInput,
    second: RuntimeInventoryInput,
) -> RuntimeInventoryInput:
    """Merge two runtime inputs while rejecting every duplicate dataset key."""
    if type(first) is not RuntimeInventoryInput or type(second) is not RuntimeInventoryInput:
        raise CoreRuntimeBundleError("Home Assistant Core runtime inventory evidence is invalid")

    return RuntimeInventoryInput(
        manifest=_merge_mapping("manifest", first.manifest, second.manifest),
        homeassistant=_merge_mapping(
            "Home Assistant",
            first.homeassistant,
            second.homeassistant,
        ),
        supervisor=_merge_mapping("Supervisor", first.supervisor, second.supervisor),
        hardware=_merge_mapping("hardware", first.hardware, second.hardware),
        analysis=_merge_mapping("analysis", first.analysis, second.analysis),
        deployments=_merge_mapping("deployments", first.deployments, second.deployments),
    )


def _merge_mapping(
    section: str,
    first: Mapping[str, object],
    second: Mapping[str, object],
) -> dict[str, object]:
    if any(not isinstance(key, str) for key in first) or any(
        not isinstance(key, str) for key in second
    ):
        raise CoreRuntimeBundleError(
            f"Home Assistant Core runtime {section} dataset keys are invalid"
        )
    duplicates = set(first).intersection(second)
    if duplicates:
        raise CoreRuntimeBundleError(
            f"Home Assistant Core runtime {section} datasets overlap"
        )
    return {**first, **second}
