"""The Experiment Engine.

Every optimisation becomes a measurable business experiment: a hypothesis
about one changed variable, a success metric, and — once it ends — a result, a
statistical confidence (where computable), and a learning.

Guarantees:

* **No duplicate experiments** — only one *active* experiment per
  (product, variable) at a time.
* On completion the result, confidence, and learning are stored, and a winning
  experiment is **promoted into company knowledge**.
* Completed experiments inform future decisions: the Product Optimiser avoids
  re-running a variable already under test, steers away from variables that
  lost, and leans into ones that won — which flows into the CEO via the
  proposal it reviews.

No dashboards, no publishing changes, no advertising — this engine records and
reasons about experiments only.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from onassis.config import Config
from onassis.database import Database
from onassis.logger import get_logger

log = get_logger(__name__)

# Canonical variables an experiment can change.
VARIABLES = (
    "title", "description", "thumbnail", "images", "mockup", "price",
    "keywords", "design", "pinterest_campaign",
)


class ExperimentError(ValueError):
    """Raised for invalid experiment operations (e.g. a duplicate)."""


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _proportion_confidence(b_success: int, b_n: int, v_success: int, v_n: int) -> float | None:
    """Two-proportion z-test → confidence (0-100) that the variant differs."""
    if b_n <= 0 or v_n <= 0:
        return None
    p1, p2 = b_success / b_n, v_success / v_n
    pooled = (b_success + v_success) / (b_n + v_n)
    se = math.sqrt(pooled * (1 - pooled) * (1 / b_n + 1 / v_n))
    if se == 0:
        return None
    z = (p2 - p1) / se
    p_value = 2 * (1 - _norm_cdf(abs(z)))
    return round(max(0.0, min(100.0, (1 - p_value) * 100)), 1)


class ExperimentEngine:
    """Starts, completes, and reasons about optimisation experiments."""

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db

    # --- Lifecycle --------------------------------------------------

    def start(
        self,
        *,
        product_id: str,
        variable: str,
        hypothesis: str,
        success_metric: str,
        expected_outcome: str = "",
        campaign_id: int | None = None,
        baseline_value: float | None = None,
        start_date: str | None = None,
    ) -> dict[str, Any]:
        """Start an experiment. Refuses a duplicate active test on the variable."""
        if variable not in VARIABLES:
            raise ExperimentError(f"Unknown variable {variable!r}.")
        if self.db.get_active_experiment(product_id, variable):
            raise ExperimentError(
                f"An active experiment already exists for {product_id}/{variable}."
            )
        exp = {
            "product_id": product_id,
            "campaign_id": campaign_id,
            "hypothesis": hypothesis,
            "variable": variable,
            "expected_outcome": expected_outcome,
            "success_metric": success_metric,
            "start_date": start_date or date.today().isoformat(),
            "status": "active",
            "baseline_value": baseline_value,
        }
        exp["id"] = self.db.insert_experiment(exp)
        return exp

    def complete(
        self,
        experiment_id: int,
        *,
        result_value: float,
        baseline_value: float | None = None,
        higher_is_better: bool = True,
        samples: dict[str, int] | None = None,
        learning: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Complete an experiment: store result, confidence, and learning.

        ``samples`` (``baseline_n``/``baseline_success``/``variant_n``/
        ``variant_success``) enables a statistical confidence for rate metrics.
        A win is promoted into company knowledge.
        """
        exp = self.db.get_experiment(experiment_id)
        if exp is None:
            raise ExperimentError(f"No experiment with id {experiment_id}.")
        if exp["status"] != "active":
            raise ExperimentError(f"Experiment #{experiment_id} is not active.")

        baseline = baseline_value if baseline_value is not None else exp.get("baseline_value")
        result = self._result(baseline, result_value, higher_is_better)
        confidence = None
        if samples:
            confidence = _proportion_confidence(
                samples.get("baseline_success", 0), samples.get("baseline_n", 0),
                samples.get("variant_success", 0), samples.get("variant_n", 0),
            )
        learning = learning or self._auto_learning(exp, baseline, result_value, result)

        self.db.update_experiment(
            experiment_id,
            {
                "status": "completed",
                "result": result,
                "learning": learning,
                "end_date": end_date or date.today().isoformat(),
                "baseline_value": baseline,
                "result_value": result_value,
                "confidence": confidence,
                "promoted": 1 if result == "win" else 0,  # winners become knowledge
            },
        )
        updated = self.db.get_experiment(experiment_id)
        log.info("Completed experiment #%s: %s (confidence %s)", experiment_id,
                 result, confidence)
        assert updated is not None
        return updated

    def abandon(self, experiment_id: int) -> dict[str, Any]:
        exp = self.db.get_experiment(experiment_id)
        if exp is None:
            raise ExperimentError(f"No experiment with id {experiment_id}.")
        self.db.update_experiment(experiment_id, {"status": "abandoned",
                                                  "end_date": date.today().isoformat()})
        return self.db.get_experiment(experiment_id)  # type: ignore[return-value]

    # --- Reads ------------------------------------------------------

    def get(self, experiment_id: int) -> dict[str, Any] | None:
        return self.db.get_experiment(experiment_id)

    def list(self) -> list[dict[str, Any]]:
        return self.db.list_experiments()

    def active(self) -> list[dict[str, Any]]:
        return self.db.list_active_experiments()

    def promoted_learnings(self) -> list[dict[str, Any]]:
        return self.db.list_promoted_learnings()

    # --- Decision support (used by the optimiser / CEO) -------------

    def has_active(self, product_id: str, variable: str) -> bool:
        return self.db.get_active_experiment(product_id, variable) is not None

    def last_result(self, product_id: str, variable: str) -> str | None:
        exp = self.db.get_last_completed_experiment(product_id, variable)
        return exp["result"] if exp else None

    # --- Helpers ----------------------------------------------------

    @staticmethod
    def _result(baseline: float | None, result_value: float, higher_is_better: bool) -> str:
        if baseline is None or result_value == baseline:
            return "inconclusive" if baseline is not None else "win" if result_value > 0 else "inconclusive"
        improved = result_value > baseline
        return "win" if improved == higher_is_better else "loss"

    @staticmethod
    def _auto_learning(
        exp: dict[str, Any], baseline: float | None, result_value: float, result: str
    ) -> str:
        base = "n/a" if baseline is None else f"{baseline:g}"
        return (
            f"Changing '{exp['variable']}' moved {exp.get('success_metric', 'the metric')} "
            f"from {base} to {result_value:g} ({result})."
        )
