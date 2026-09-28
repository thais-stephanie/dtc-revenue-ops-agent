"""The publication gate.

Everything the model returns is untrusted until it passes here. Evaluation
tells us how often the model gets it wrong; this decides what happens when it
does. A rejected recommendation is logged and never reaches the brief.

Note what is deliberately NOT enforced here: a customer asking to pause
expansion. That constraint lives in free text, and catching it is the model's
job, which is exactly what the Cinder Supply eval case measures. Hard-coding it
would make that case prove nothing. The gate only enforces that the activity
was READ before any expansion, never how it was interpreted.
"""
from __future__ import annotations

from pydantic import ValidationError

from . import config
from .confidence import derive_confidence, explain_confidence
from .domain import EXPANSION_ACTIONS, Action, ReconciliationStatus
from .schemas import (
    DailyBrief,
    PublishedRecommendation,
    Recommendation,
    Rejection,
)


def validate_brief(raw: dict) -> tuple[DailyBrief | None, Rejection | None]:
    try:
        return DailyBrief.model_validate(raw), None
    except ValidationError as exc:
        return None, Rejection(
            account_id=None,
            reason="schema_validation_failed",
            details=[f"{e['loc']}: {e['msg']}" for e in exc.errors()][:8],
        )


def gate_recommendation(
    rec: Recommendation,
    *,
    facts: dict,
    available_source_refs: set[str],
    signal_severity: int,
    competing_signals: bool,
    source_ref_provenance: dict[str, dict],
) -> tuple[PublishedRecommendation | None, Rejection | None]:
    """Fail closed. A recommendation only publishes if every check passes.

    `available_source_refs` must be the refs issued for `rec.account_id` only.
    `source_ref_provenance` (every ref issued this run) never makes a ref
    valid; it only names the rejection when a real ref belongs to another account.
    """
    provenance = source_ref_provenance

    def foreign(ref: str) -> bool:
        return ref in provenance and provenance[ref]["account_id"] != rec.account_id

    invalid = sorted(set(rec.source_refs) - available_source_refs)
    if invalid:
        cross = any(foreign(ref) for ref in invalid)
        return None, Rejection(
            account_id=rec.account_id,
            reason="cross_account_source_reference" if cross else "unknown_source_reference",
            details=invalid,
        )

    recon_status = ReconciliationStatus(facts["reconciliation"]["status"])
    freshness_hours = float(facts["data_freshness"]["commerce_hours"])

    if rec.recommended_action in EXPANSION_ACTIONS:
        if recon_status is ReconciliationStatus.EXCEPTION:
            return None, Rejection(
                account_id=rec.account_id,
                reason="expansion_blocked_by_reconciliation",
                details=[
                    f"reported {facts['reconciliation']['reported_attributed_revenue']}",
                    f"commerce {facts['reconciliation']['commerce_attributed_revenue']}",
                    f"relative delta {facts['reconciliation']['relative_delta']:.1%}",
                ],
            )
        if freshness_hours > config.STALE_AFTER_HOURS:
            return None, Rejection(
                account_id=rec.account_id,
                reason="expansion_blocked_by_stale_data",
                details=[f"commerce data is {freshness_hours:.0f}h old"],
            )
        activity_read = any(
            provenance[ref]["origin"] == "tool:get_recent_account_activity"
            for ref in available_source_refs
        )
        if not activity_read:
            return None, Rejection(
                account_id=rec.account_id,
                reason="expansion_without_activity_read",
                details=["no successful get_recent_account_activity for this account this run"],
            )

    task_ref = f"proposed_task:{rec.proposed_task_id}"
    if rec.proposed_task_id and task_ref not in available_source_refs:
        return None, Rejection(
            account_id=rec.account_id,
            reason="cross_account_proposed_task" if foreign(task_ref) else "unknown_proposed_task",
            details=[rec.proposed_task_id],
        )

    confidence = derive_confidence(
        signal_severity=signal_severity,
        freshness_hours=freshness_hours,
        reconciliation_status=recon_status,
        conflicting_context=competing_signals,
    )
    reason = explain_confidence(
        signal_severity=signal_severity,
        freshness_hours=freshness_hours,
        reconciliation_status=recon_status,
        conflicting_context=competing_signals,
    )
    return (
        PublishedRecommendation(
            recommendation=rec,
            confidence=confidence.value,
            confidence_reason=reason,
        ),
        None,
    )


def blocked_actions(facts: dict) -> set[Action]:
    """Actions the gate will refuse for this account, given today's facts."""
    recon_status = ReconciliationStatus(facts["reconciliation"]["status"])
    stale = float(facts["data_freshness"]["commerce_hours"]) > config.STALE_AFTER_HOURS
    if recon_status is ReconciliationStatus.EXCEPTION or stale:
        return set(EXPANSION_ACTIONS)
    return set()
