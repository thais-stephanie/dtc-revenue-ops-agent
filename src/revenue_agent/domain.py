"""Domain types. Plain stdlib so the deterministic core has no dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class ReconciliationStatus(str, Enum):
    MATCH = "match"
    PROBABLE_MATCH = "probable_match"
    EXCEPTION = "exception"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalType(str, Enum):
    CART_RECOVERY_GAP = "cart_recovery_gap"
    REACTIVATION_GAP = "reactivation_gap"
    PERFORMANCE_DROP = "performance_drop"
    ATTRIBUTION_EXCEPTION = "attribution_exception"
    STALE_DATA = "stale_data"
    BILLING_HOLD = "billing_hold"
    CREATIVE_FATIGUE = "creative_fatigue"
    ENGAGEMENT_GAP = "engagement_gap"


class Action(str, Enum):
    EXPAND_CART_RECOVERY = "EXPAND_CART_RECOVERY"
    LAUNCH_REACTIVATION = "LAUNCH_REACTIVATION"
    PREPARE_ACCOUNT_REVIEW = "PREPARE_ACCOUNT_REVIEW"
    INVESTIGATE_ATTRIBUTION = "INVESTIGATE_ATTRIBUTION"
    REFRESH_DATA = "REFRESH_DATA"
    COORDINATE_BILLING = "COORDINATE_BILLING"
    REFRESH_CREATIVE = "REFRESH_CREATIVE"
    INVESTIGATE_BEFORE_EXPANSION = "INVESTIGATE_BEFORE_EXPANSION"
    NO_ACTION = "NO_ACTION"


#: Actions that commit more customer spend. Blocked when the facts don't hold up.
EXPANSION_ACTIONS = frozenset(
    {Action.EXPAND_CART_RECOVERY, Action.LAUNCH_REACTIVATION}
)


@dataclass(frozen=True)
class Account:
    id: str
    brand_name: str
    vertical: str
    commerce_platform: str
    lifecycle_stage: str
    account_owner: str
    created_at: datetime


@dataclass(frozen=True)
class Campaign:
    id: str
    account_id: str
    campaign_type: str          # prospecting | retargeting | cart_recovery | win_back | replenishment
    status: str                 # active | ended
    creative_id: str
    launched_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class CampaignMetric:
    campaign_id: str
    metric_date: date
    mailed: int
    spend: float
    attributed_revenue: float
    conversions: int

    @property
    def response_rate(self) -> float:
        return round(self.conversions / self.mailed, 5) if self.mailed else 0.0


@dataclass(frozen=True)
class CommerceMetric:
    account_id: str
    metric_date: date
    gross_revenue: float
    attributed_direct_mail_revenue: float
    orders: int
    abandoned_carts: int
    lapsed_90d_customers: int
    active_customers: int
    synced_at: datetime


@dataclass(frozen=True)
class CrmNote:
    id: str
    account_id: str
    activity_type: str          # note | call | email | meeting
    occurred_at: datetime
    author: str
    body: str


@dataclass(frozen=True)
class Invoice:
    id: str
    account_id: str
    amount: float
    due_date: date
    paid_at: date | None
    status: str                 # paid | open | overdue


@dataclass
class Portfolio:
    accounts: list[Account] = field(default_factory=list)
    campaigns: list[Campaign] = field(default_factory=list)
    campaign_metrics: list[CampaignMetric] = field(default_factory=list)
    commerce_metrics: list[CommerceMetric] = field(default_factory=list)
    crm_notes: list[CrmNote] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)


@dataclass(frozen=True)
class Signal:
    account_id: str
    signal_type: SignalType
    severity: int               # 1..5, deterministic
    facts: dict

    @property
    def source_ref(self) -> str:
        return f"signal:{self.account_id}:{self.signal_type.value}"


@dataclass
class ProposedTask:
    id: str
    account_id: str
    title: str
    description: str
    idempotency_key: str
    status: str = "PROPOSED"    # PROPOSED | APPROVED | EXECUTED | REJECTED
