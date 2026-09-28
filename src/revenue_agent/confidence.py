"""Confidence is derived, never generated.

The model has no `confidence` field in its output schema. The publication gate
computes it from data freshness, reconciliation status, signal strength and
whether the account context contains a known conflict.
"""
from __future__ import annotations

from . import config
from .domain import Confidence, ReconciliationStatus


def derive_confidence(
    *,
    signal_severity: int,
    freshness_hours: float,
    reconciliation_status: ReconciliationStatus,
    conflicting_context: bool = False,
) -> Confidence:
    if (
        reconciliation_status is ReconciliationStatus.EXCEPTION
        or freshness_hours > config.STALE_AFTER_HOURS
        or conflicting_context
    ):
        return Confidence.LOW

    if (
        reconciliation_status is ReconciliationStatus.PROBABLE_MATCH
        or signal_severity < 3
        or freshness_hours > config.DEGRADED_AFTER_HOURS
    ):
        return Confidence.MEDIUM

    return Confidence.HIGH


def explain_confidence(
    *,
    signal_severity: int,
    freshness_hours: float,
    reconciliation_status: ReconciliationStatus,
    conflicting_context: bool = False,
) -> str:
    """Human-readable reason, shown in the brief next to the badge."""
    reasons: list[str] = []
    if reconciliation_status is ReconciliationStatus.EXCEPTION:
        reasons.append("revenue reconciliation is an exception")
    elif reconciliation_status is ReconciliationStatus.PROBABLE_MATCH:
        reasons.append("revenue reconciliation is a probable match")
    if freshness_hours > config.STALE_AFTER_HOURS:
        reasons.append(f"commerce data is {freshness_hours:.0f}h old")
    elif freshness_hours > config.DEGRADED_AFTER_HOURS:
        reasons.append(f"commerce data is {freshness_hours:.0f}h old")
    if conflicting_context:
        reasons.append("the account carries competing signals")
    if signal_severity < 3:
        reasons.append("the underlying signal is weak")
    if not reasons:
        reasons.append("facts are fresh, reconciled and the signal is strong")
    return "; ".join(reasons)
