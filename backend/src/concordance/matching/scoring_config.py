"""The fitted configuration, as one serializable record.

This is deliberately the exact shape of a `scoring_configs` row (PLAN 6), not a
convenient local format that a loader will have to reshape at Stage 5. The JSON
written to `data/configs/` today is the JSON that becomes the row: `params`,
`calibrator` and `thresholds` are each keyed by model kind, which is how PLAN
11.2's "two independent fits stored as separate keys in one config row" is
honoured without two rows and without a shared block that lets one model's
statistics leak into the other's.

Everything needed to reproduce the fit travels with it - seed, corruption
level, dataset content hashes, the EM convergence trace - because a
configuration that cannot be traced back to the data it was fitted on is a
magic number with extra steps.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from concordance.matching.comparators import ModelKind
from concordance.matching.scorer import (
    DEFAULT_MARGIN_DELTA,
    DEFAULT_TOP_K,
    MatchingEngine,
    ModelBundle,
)

CONFIG_SCHEMA_VERSION = 1


@dataclass
class ScoringConfig:
    """One fitted configuration: both models, their calibrators, their thresholds."""

    version: int = CONFIG_SCHEMA_VERSION
    config_id: str = ""
    fitted_at: str = ""
    fitted_from: str = "em"
    engine_version: str = ""
    seed: int = 0
    top_k: int = DEFAULT_TOP_K
    margin_delta: float = DEFAULT_MARGIN_DELTA
    bundles: dict[ModelKind, ModelBundle] = field(default_factory=dict)
    dataset: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- use -------------------------------------------------------------
    def engine(self) -> MatchingEngine:
        """The scorer this configuration describes."""
        return MatchingEngine(
            individual=self.bundles[ModelKind.INDIVIDUAL],
            organization=self.bundles[ModelKind.ORGANIZATION],
            top_k=self.top_k,
            margin_delta=self.margin_delta,
        )

    # -- serialization ---------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "config_id": self.config_id,
            "fitted_at": self.fitted_at,
            "fitted_from": self.fitted_from,
            "engine_version": self.engine_version,
            "seed": self.seed,
            "top_k": self.top_k,
            "margin_delta": self.margin_delta,
            "params": {str(k): b.model.to_dict() for k, b in self.bundles.items()},
            "calibrator": {str(k): b.calibrator.as_dict() for k, b in self.bundles.items()},
            "thresholds": {str(k): b.thresholds.as_dict() for k, b in self.bundles.items()},
            "calibration": {str(k): b.calibration for k, b in self.bundles.items()},
            "dataset": self.dataset,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ScoringConfig:
        bundles: dict[ModelKind, ModelBundle] = {}
        for key in payload["params"]:
            kind = ModelKind(key)
            bundles[kind] = ModelBundle.from_dict(
                {
                    "model": payload["params"][key],
                    "calibrator": payload["calibrator"][key],
                    "thresholds": payload["thresholds"][key],
                    "calibration": payload.get("calibration", {}).get(key, {}),
                }
            )
        return cls(
            version=int(payload.get("version", CONFIG_SCHEMA_VERSION)),
            config_id=str(payload.get("config_id", "")),
            fitted_at=str(payload.get("fitted_at", "")),
            fitted_from=str(payload.get("fitted_from", "em")),
            engine_version=str(payload.get("engine_version", "")),
            seed=int(payload.get("seed", 0)),
            top_k=int(payload.get("top_k", DEFAULT_TOP_K)),
            margin_delta=float(payload.get("margin_delta", DEFAULT_MARGIN_DELTA)),
            bundles=bundles,
            dataset=dict(payload.get("dataset", {})),
            notes=list(payload.get("notes", [])),
        )

    # -- files -----------------------------------------------------------
    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    @classmethod
    def read(cls, path: Path) -> ScoringConfig:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def config_filename(corruption: float, seed: int) -> str:
    """Versioned, self-describing, and sortable: `config_c0.50_s20260914.json`."""
    return f"config_c{corruption:.2f}_s{seed}.json"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
