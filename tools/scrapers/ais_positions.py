"""Coordinated public facade for the SeaSearcher AIS Positions scraper."""

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

_impl = importlib.import_module("tools.scrapers.ais_positions_impl")


def parallel_scraping_ais_positions(llino_list, max_workers=1, driver_opts=None, config=None):
    targets = list(llino_list or [])
    if not targets:
        return _impl.parallel_scraping_ais_positions(
            targets, max_workers=max_workers, driver_opts=driver_opts, config=config
        )

    cfg = {**(config or {})}
    account_id = infer_seasearcher_account_id(cfg)
    try:
        _, skipped, _, _ = _impl._apply_upfront_local_skip_filter(targets, cfg)
        initial_completed = len(skipped)
    except Exception:
        initial_completed = 0

    description = f"AIS Positions: {cfg.get('period', 'all')}"
    with coordinated_job(
        account_id=account_id,
        job_type="ais_positions",
        total=len(targets),
        description=description,
    ) as coordinator_job:
        with track_unique_function_calls(
            _impl,
            "scraping_ais_positions",
            coordinator_job,
            total=len(targets),
            initial_completed=initial_completed,
            key_arg_index=1,
        ):
            return _impl.parallel_scraping_ais_positions(
                targets,
                max_workers=max_workers,
                driver_opts=driver_opts,
                config=cfg,
            )


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
if "parallel_scraping_ais_positions" not in __all__:
    __all__.append("parallel_scraping_ais_positions")

sys.modules[__name__].__class__ = _ImplementationProxy
