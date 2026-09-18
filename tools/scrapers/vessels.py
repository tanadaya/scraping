"""Coordinated public facade for the SeaSearcher Vessels exporter."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

from tools.coordinator.coordinator import (
    coordinated_job,
    infer_seasearcher_account_id,
    track_unique_function_calls,
)

_impl = importlib.import_module("tools.scrapers.vessels_impl")


def export_vessels_filters(filter_labels, config=None):
    labels = list(filter_labels or [])
    if not labels:
        return _impl.export_vessels_filters(labels, config)

    cfg = {**(config or {})}
    account_id = infer_seasearcher_account_id(cfg)
    with coordinated_job(
        account_id=account_id,
        job_type="vessels_export",
        total=len(labels),
        description=f"Vessels export: {len(labels)} filters",
    ) as coordinator_job:
        with track_unique_function_calls(
            _impl,
            "_merge_pages",
            coordinator_job,
            total=len(labels),
            initial_completed=0,
            key_arg_index=1,
        ):
            return _impl.export_vessels_filters(labels, cfg)


class _ImplementationProxy(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        return getattr(_impl, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("__") or name in self.__dict__:
            super().__setattr__(name, value)
        else:
            setattr(_impl, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__dict__:
            super().__delattr__(name)
        else:
            delattr(_impl, name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(dir(_impl)))


__all__ = list(getattr(_impl, "__all__", [name for name in dir(_impl) if not name.startswith("_")]))
if "export_vessels_filters" not in __all__:
    __all__.append("export_vessels_filters")

sys.modules[__name__].__class__ = _ImplementationProxy
