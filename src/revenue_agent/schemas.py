"""The contract the model must satisfy.

Note what is NOT here: `confidence`. The model cannot express certainty in its
output at all. The publication gate derives it from facts and attaches it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .domain import Action


class Recommendation(BaseModel):
    account_id: str
    priority: str = Field(pattern="^(high|medium|low)$")
    category: str = Field(pattern="^(growth|risk|data_quality|billing|creative)$")
    headline: str = Field(min_length=10, max_length=160)
    rationale: str = Field(min_length=20, max_length=900)
    recommended_action: Action
    source_refs: list[str] = Field(min_length=1)
    blockers: list[str] = Field(default_factory=list)
    acknowledged_tension: str | None = None
    proposed_task_id: str | None = None

    @field_validator("source_refs")
    @classmethod
    def _dedupe(cls, refs: list[str]) -> list[str]:
        return sorted(set(refs))


class DailyBrief(BaseModel):
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=5)
    investigated_accounts: int = 0
    notes: str | None = None


class PublishedRecommendation(BaseModel):
    """A recommendation that passed the gate, with derived fields attached."""

    recommendation: Recommendation
    confidence: str
    confidence_reason: str


class Rejection(BaseModel):
    account_id: str | None
    reason: str
    details: list[str] = Field(default_factory=list)
    #: where in the pipeline it was refused: loop, schema, limit, shortlist, gate
    stage: str | None = None
    #: position in raw_brief["recommendations"], when a recommendation was refused
    recommendation_index: int | None = None
    #: the validated recommendation that was refused, so "model said" survives
    recommendation: dict | None = None


class RunTelemetry(BaseModel):
    accounts_scanned: int = 0
    accounts_shortlisted: int = 0
    accounts_recommended: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: int = 0


class RunResult(BaseModel):
    published: list[PublishedRecommendation] = Field(default_factory=list)
    rejected: list[Rejection] = Field(default_factory=list)
    telemetry: RunTelemetry = Field(default_factory=RunTelemetry)
    tool_log: list[dict] = Field(default_factory=list)
    raw_brief: dict | None = None
    #: forensics: what was asked for, what the provider said it served, per turn
    model_requested: str | None = None
    turns: list[dict] = Field(default_factory=list)
    #: every source_ref issued to the model this run: shortlist signals + tools
    available_source_refs: list[str] = Field(default_factory=list)
    #: ref -> {"account_id": issued for, "origin": "shortlist_signal" | "tool:<name>"}
    source_ref_provenance: dict[str, dict] = Field(default_factory=dict)
    #: the deterministic shortlist exactly as handed to the model
    shortlist_supplied: list[dict] = Field(default_factory=list)
    #: which prompt and tool contract produced this run
    system_prompt_sha256: str | None = None
    tool_definitions_sha256: str | None = None
