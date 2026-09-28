"""Central configuration. Every threshold used by a deterministic rule lives here.

These are DEMO POLICY values. In a real deployment Finance/RevOps owns them.
They are defined before any agent run, never tuned to make a run look better.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

# The demo is pinned to a fixed "as of" instant so the dataset never rots
# and every run is reproducible.
AS_OF = datetime.fromisoformat(
    os.getenv("DEMO_AS_OF", "2026-09-22T09:00:00+00:00")
).astimezone(timezone.utc)

DATASET_SEED = int(os.getenv("DEMO_SEED", "42"))
HISTORY_DAYS = 90

# --- reconciliation tolerance bands -------------------------------------
MATCH_ABS_USD = 250.0
MATCH_REL = 0.03
PROBABLE_ABS_USD = 1_000.0
PROBABLE_REL = 0.08

# --- data freshness ------------------------------------------------------
STALE_AFTER_HOURS = 48.0
DEGRADED_AFTER_HOURS = 24.0

# --- signal thresholds ---------------------------------------------------
PERFORMANCE_DROP_REL = 0.30      # 7d ROAS vs previous 28d baseline
CART_VOLUME_MIN = 500            # abandoned carts in 30d to call it an opportunity
LAPSED_CUSTOMERS_MIN = 2_000     # lapsed >90d customers to call it a reactivation gap
ENGAGEMENT_GAP_DAYS = 45         # days since last AM touch
CREATIVE_FATIGUE_MIN_CAMPAIGNS = 3
BILLING_OVERDUE_DAYS = 30

# --- agent budget --------------------------------------------------------
MAX_TOOL_CALLS_PER_RUN = 14           # every attempt, reads and proposals alike
# Reading and proposing are different capabilities with separate allowances.
# One shared per-account budget of 4 made the contract impossible: the four
# read tools consumed it, leaving no room for the task proposal.
MAX_READ_TOOL_CALLS_PER_ACCOUNT = 4   # the four get_* tools
MAX_TASK_PROPOSALS_PER_ACCOUNT = 1    # propose_crm_task
# submit_daily_brief is the terminal submission and consumes neither.
MAX_SHORTLIST = 8
MAX_RECOMMENDATIONS = 3

# --- unattended loop guards (unattended runs only) -------------------------
# A best-effort run budget, NOT a provider-side spending cap: before each model
# call the loop stops if spent + a conservative estimate of that call exceeds it.
UNATTENDED_MAX_COST_USD = float(os.getenv("UNATTENDED_MAX_COST_USD", "0.50"))
# Stop after this many refused tool calls in a row; any successful call resets it.
UNATTENDED_MAX_REFUSAL_STREAK = int(os.getenv("UNATTENDED_MAX_REFUSAL_STREAK", "3"))
# Model calls per turn, first try included; only transient provider errors retry.
UNATTENDED_MAX_MODEL_ATTEMPTS = int(os.getenv("UNATTENDED_MAX_MODEL_ATTEMPTS", "3"))
# Warn after this many consecutive ok runs that published nothing. DEMO default:
# the right number depends on how often a quiet day is normal, which is a
# RevOps/Operations call, not an engineering one.
UNATTENDED_SILENT_SUCCESS_STREAK = int(os.getenv("UNATTENDED_SILENT_SUCCESS_STREAK", "3"))

# --- model ---------------------------------------------------------------
# Set ANTHROPIC_MODEL to a model id your account can call.
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
# Published prices change; keep them in config so the cost report is auditable.
INPUT_USD_PER_MTOK = float(os.getenv("INPUT_USD_PER_MTOK", "3.00"))
OUTPUT_USD_PER_MTOK = float(os.getenv("OUTPUT_USD_PER_MTOK", "15.00"))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://revenue:revenue@localhost:5432/revenue")


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        input_tokens / 1_000_000 * INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * OUTPUT_USD_PER_MTOK,
        6,
    )
