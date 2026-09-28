"""Revenue reconciliation between campaign reporting and the commerce source.

The bands are declared in config before any run. The agent never decides whether
a difference is material; it only receives the classification as a fact.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .domain import ReconciliationStatus


@dataclass(frozen=True)
class ReconciliationResult:
    reported: float
    commerce: float
    absolute_delta: float
    relative_delta: float
    status: ReconciliationStatus

    def as_facts(self) -> dict:
        return {
            "reported_attributed_revenue": round(self.reported, 2),
            "commerce_attributed_revenue": round(self.commerce, 2),
            "absolute_delta": round(self.absolute_delta, 2),
            "relative_delta": round(self.relative_delta, 4),
            "status": self.status.value,
            "policy": {
                "match": f"<= ${config.MATCH_ABS_USD:,.0f} or <= {config.MATCH_REL:.0%}",
                "probable_match": f"<= ${config.PROBABLE_ABS_USD:,.0f} or <= {config.PROBABLE_REL:.0%}",
                "exception": "above both bands",
            },
        }


def classify_revenue_difference(reported: float, commerce: float) -> ReconciliationResult:
    absolute_delta = abs(reported - commerce)

    if commerce == 0:
        relative_delta = 1.0 if reported else 0.0
    else:
        relative_delta = absolute_delta / commerce

    if absolute_delta <= config.MATCH_ABS_USD or relative_delta <= config.MATCH_REL:
        status = ReconciliationStatus.MATCH
    elif absolute_delta <= config.PROBABLE_ABS_USD or relative_delta <= config.PROBABLE_REL:
        status = ReconciliationStatus.PROBABLE_MATCH
    else:
        status = ReconciliationStatus.EXCEPTION

    return ReconciliationResult(
        reported=reported,
        commerce=commerce,
        absolute_delta=absolute_delta,
        relative_delta=relative_delta,
        status=status,
    )
