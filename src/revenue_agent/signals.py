"""Deterministic signal detection and shortlisting.

The model never scans the portfolio. This module decides which accounts are
even worth investigating, and hands the agent a short list with the facts that
put each account there.
"""
from __future__ import annotations

from . import config
from .domain import Signal, SignalType
from .repository import Repository


def detect(facts: dict) -> list[Signal]:
    account_id = facts["account"]["id"]
    out: list[Signal] = []
    perf = facts["performance"]
    commerce = facts["commerce"]

    # 1. attribution exception -------------------------------------------
    if facts["reconciliation"]["status"] == "exception":
        out.append(
            Signal(
                account_id,
                SignalType.ATTRIBUTION_EXCEPTION,
                5,
                {
                    "reported": facts["reconciliation"]["reported_attributed_revenue"],
                    "commerce": facts["reconciliation"]["commerce_attributed_revenue"],
                    "relative_delta": facts["reconciliation"]["relative_delta"],
                },
            )
        )

    # 2. stale commerce data ---------------------------------------------
    if facts["data_freshness"]["stale"]:
        out.append(
            Signal(
                account_id,
                SignalType.STALE_DATA,
                4,
                {"data_age_hours": facts["data_freshness"]["commerce_hours"]},
            )
        )

    # 3. performance drop against the 28-day baseline ---------------------
    roas_7, baseline = perf["roas_7d"], perf["roas_baseline_28d"]
    if roas_7 and baseline:
        drop = (baseline - roas_7) / baseline
        if drop >= config.PERFORMANCE_DROP_REL:
            out.append(
                Signal(
                    account_id,
                    SignalType.PERFORMANCE_DROP,
                    5 if drop >= 0.5 else 4,
                    {
                        "roas_7d": roas_7,
                        "roas_baseline_28d": baseline,
                        "relative_drop": round(drop, 3),
                    },
                )
            )

    # 4. cart recovery gap ------------------------------------------------
    if (
        commerce["abandoned_carts_30d"] >= config.CART_VOLUME_MIN
        and "cart_recovery" not in perf["active_campaign_types"]
    ):
        out.append(
            Signal(
                account_id,
                SignalType.CART_RECOVERY_GAP,
                4 if commerce["abandoned_carts_30d"] >= 1_000 else 3,
                {
                    "abandoned_carts_30d": commerce["abandoned_carts_30d"],
                    "abandoned_cart_growth": commerce["abandoned_cart_growth"],
                    "active_campaign_types": perf["active_campaign_types"],
                },
            )
        )

    # 5. reactivation gap -------------------------------------------------
    if (
        commerce["lapsed_90d_customers"] >= config.LAPSED_CUSTOMERS_MIN
        and "win_back" not in perf["active_campaign_types"]
    ):
        out.append(
            Signal(
                account_id,
                SignalType.REACTIVATION_GAP,
                4 if commerce["lapsed_90d_customers"] >= 4_000 else 3,
                {
                    "lapsed_90d_customers": commerce["lapsed_90d_customers"],
                    "active_campaign_types": perf["active_campaign_types"],
                },
            )
        )

    # 6. billing hold -----------------------------------------------------
    if facts["billing"]["days_overdue"] >= config.BILLING_OVERDUE_DAYS:
        out.append(
            Signal(
                account_id,
                SignalType.BILLING_HOLD,
                4,
                {
                    "days_overdue": facts["billing"]["days_overdue"],
                    "amount_overdue": facts["billing"]["amount_overdue"],
                },
            )
        )

    # 7. creative fatigue -------------------------------------------------
    if facts["creative_fatigue"]:
        out.append(
            Signal(account_id, SignalType.CREATIVE_FATIGUE, 3, dict(facts["creative_fatigue"]))
        )

    # 8. engagement gap ---------------------------------------------------
    if facts["engagement"]["days_since_last_touch"] >= config.ENGAGEMENT_GAP_DAYS:
        out.append(
            Signal(
                account_id,
                SignalType.ENGAGEMENT_GAP,
                2,
                {"days_since_last_touch": facts["engagement"]["days_since_last_touch"]},
            )
        )

    return out


def scan(repo: Repository) -> dict[str, list[Signal]]:
    return {
        account_id: detect(repo.account_facts(account_id))
        for account_id in repo.list_account_ids()
    }


def shortlist(repo: Repository, limit: int = config.MAX_SHORTLIST) -> list[dict]:
    """Rank accounts by strongest signal, then by how many signals they carry."""
    scanned = scan(repo)
    ranked = []
    for account_id, signals in scanned.items():
        if not signals:
            continue
        top = max(s.severity for s in signals)
        ranked.append(
            {
                "account_id": account_id,
                "top_severity": top,
                "signal_count": len(signals),
                "signals": [
                    {
                        "type": s.signal_type.value,
                        "severity": s.severity,
                        "facts": s.facts,
                        "source_ref": s.source_ref,
                    }
                    for s in sorted(signals, key=lambda s: -s.severity)
                ],
                "competing_signals": _has_competing_signals(signals),
            }
        )
    ranked.sort(key=lambda r: (-r["top_severity"], -r["signal_count"], r["account_id"]))
    return ranked[:limit]


def _has_competing_signals(signals) -> bool:
    """True when the account shows a growth opportunity AND a judgement-level risk.

    Deliberately narrow. Stale data, attribution exceptions and billing holds are
    hard blocks handled by the publication gate, not genuine ambiguity. A growth
    opportunity sitting next to falling performance is a real judgement call, and
    that is what lowers confidence.
    """
    types = {s.signal_type for s in signals}
    growth = {SignalType.CART_RECOVERY_GAP, SignalType.REACTIVATION_GAP}
    judgement_risk = {SignalType.PERFORMANCE_DROP, SignalType.CREATIVE_FATIGUE}
    return bool(types & growth) and bool(types & judgement_risk)
