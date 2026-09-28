"""The agent loop.

Plain Claude tool calling, no framework. The model client is injected so the
eval suite can run the whole pipeline against a scripted client with no network
and no spend, and the demo can run against the real SDK.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol

from . import config, guardrails, prompts, signals
from .repository import Repository
from .schemas import Rejection, RunResult, RunTelemetry
from .tools import TOOL_DEFINITIONS, Refused, ToolError, ToolRuntime

MAX_ITERATIONS = 20

#: HTTP statuses worth retrying: the same set the Anthropic SDK retries
TRANSIENT_STATUS = {408, 409, 429}
TRANSIENT_ERRORS = {"APITimeoutError", "APIConnectionError"}  # SDK names, no SDK import


def is_transient(exc: BaseException) -> bool:
    """A provider hiccup a retry can fix: timeout, connection, 408/409/429, 5xx.

    Nothing else is retried. Business outcomes (gate refusals, schema, budget
    and guard stops) never raise, so they never reach this question.
    """
    status = getattr(exc, "status_code", None)
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or type(exc).__name__ in TRANSIENT_ERRORS
        or (isinstance(status, int) and (status in TRANSIENT_STATUS or status >= 500))
    )


class ModelClient(Protocol):
    """The slice of the Anthropic SDK this project uses."""

    def create_message(
        self,
        *,
        model: str,
        system: str,
        tools: list[dict],
        messages: list[dict],
        max_tokens: int,
    ) -> Any: ...


class AnthropicClient:
    """Thin adapter over the official SDK."""

    def __init__(self, api_key: str | None = None, **sdk_options) -> None:
        """`sdk_options` go to anthropic.Anthropic, e.g. max_retries, timeout."""
        import anthropic  # local import: the eval suite does not need the SDK

        if api_key:
            sdk_options["api_key"] = api_key
        self._client = anthropic.Anthropic(**sdk_options)

    def create_message(self, **kwargs):
        return self._client.messages.create(**kwargs)


def _blocks(response: Any) -> list[Any]:
    return list(getattr(response, "content", []) or [])


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _turn_record(turn: int, response: Any, tool_uses: list[Any]) -> dict:
    """One model turn, as the provider reported it. No text, no reasoning."""
    usage = getattr(response, "usage", None)
    record = {
        "turn": turn,
        "model": getattr(response, "model", None),
        "stop_reason": getattr(response, "stop_reason", None),
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "tool_uses": [b.name for b in tool_uses],
    }
    for extra in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        value = getattr(usage, extra, None)
        if isinstance(value, int):
            record[extra] = value
    return record


def _usage(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


class AgentRunner:
    def __init__(
        self,
        repo: Repository,
        client: ModelClient,
        *,
        model: str = config.MODEL,
        max_tokens: int = 4096,
        max_cost_usd: float | None = None,
        max_refusal_streak: int | None = None,
        max_model_attempts: int = 1,
    ) -> None:
        self.repo = repo
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        # unattended-only loop guards; None (the default) leaves the loop as it was
        self.max_cost_usd = max_cost_usd
        self.max_refusal_streak = max_refusal_streak
        # 1 = no retry. Unattended sets more AND turns the SDK's own retries off,
        # so this loop is the only retry policy and its count is the real one.
        self.max_model_attempts = max_model_attempts
        self.model_retries = 0
        self.model_error: BaseException | None = None

    def _create_message(self, **request):
        """One model call, retried only for transient provider errors."""
        for attempt in range(1, self.max_model_attempts + 1):
            try:
                return self.client.create_message(**request)
            except Exception as exc:
                if attempt == self.max_model_attempts or not is_transient(exc):
                    self.model_error = exc
                    raise
                self.model_retries += 1
                time.sleep(min(2 ** (attempt - 1), 4))  # 1s, 2s, 4s, 4s...

    def _next_call_worst_case_usd(self, messages: list[dict]) -> float:
        """A conservative estimate of what the next model call can cost.

        Output is exact: at most max_tokens. Input is counted in CHARACTERS of
        what will be sent, which in practice exceeds its token count several
        times over. It is an estimate, not a proof: the provider adds its own
        tool-use overhead, and it cannot see how the provider tokenises.
        """
        chars = (
            len(prompts.SYSTEM_PROMPT)
            + len(json.dumps(TOOL_DEFINITIONS))
            + len(json.dumps(messages, default=str))
        )
        return config.estimate_cost_usd(chars, self.max_tokens)

    def run(self, shortlist: list[dict] | None = None) -> RunResult:
        started = time.monotonic()
        self.model_retries, self.model_error = 0, None
        shortlist = shortlist if shortlist is not None else signals.shortlist(self.repo)
        self._shortlist = shortlist
        by_account = {entry["account_id"]: entry for entry in shortlist}
        # the refs on the shortlist are issued to the model with it, so they
        # are citable; taken from the very list the prompt serialises below
        runtime = ToolRuntime(self.repo, shortlist)
        telemetry = RunTelemetry(
            accounts_scanned=len(self.repo.list_account_ids()),
            accounts_shortlisted=len(shortlist),
        )

        messages: list[dict] = [
            {"role": "user", "content": prompts.build_daily_request(shortlist)}
        ]
        raw_brief: dict | None = None
        rejected: list[Rejection] = []
        turns: list[dict] = []
        refusal_streak = 0

        for turn in range(1, MAX_ITERATIONS + 1):
            if self.max_cost_usd is not None:
                spent = config.estimate_cost_usd(telemetry.input_tokens, telemetry.output_tokens)
                worst = self._next_call_worst_case_usd(messages)
                if spent + worst > self.max_cost_usd:
                    rejected.append(
                        Rejection(
                            account_id=None,
                            reason="unattended_cost_budget",
                            details=[
                                f"spent ${spent:.4f} + next call up to ${worst:.4f} "
                                f"> budget ${self.max_cost_usd:.2f}; stopped before calling"
                            ],
                            stage="loop",
                        )
                    )
                    break
            response = self._create_message(
                model=self.model,
                system=prompts.SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
                max_tokens=self.max_tokens,
            )
            tokens_in, tokens_out = _usage(response)
            telemetry.input_tokens += tokens_in
            telemetry.output_tokens += tokens_out

            blocks = _blocks(response)
            tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            turns.append(_turn_record(turn, response, tool_uses))
            messages.append(
                {"role": "assistant", "content": [_serialise(b) for b in blocks]}
            )

            if not tool_uses:
                break

            results = []
            submitted = False
            for block in tool_uses:
                if block.name == "submit_daily_brief":
                    raw_brief = dict(block.input)
                    submitted = True
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"status": "received"}),
                        }
                    )
                    continue
                telemetry.tool_calls += 1
                try:
                    payload = runtime.execute(block.name, dict(block.input))
                    content = json.dumps(payload, default=str)
                    refusal_streak = 0
                except Refused as exc:
                    content = json.dumps({"error": str(exc)})
                    refusal_streak += 1
                except ToolError as exc:
                    content = json.dumps({"error": str(exc)})
                except Exception as exc:  # a broken tool is a fact, not a silent zero
                    content = json.dumps({"error": f"tool failed: {exc!r}"})
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": content}
                )

            messages.append({"role": "user", "content": results})
            if submitted:
                break
            if self.max_refusal_streak and refusal_streak >= self.max_refusal_streak:
                rejected.append(
                    Rejection(
                        account_id=None,
                        reason="refusal_streak_limit",
                        details=[f"{refusal_streak} consecutive refused tool calls"],
                        stage="loop",
                    )
                )
                break

        if raw_brief is None:
            if not rejected:  # a guard that stopped the loop already said why
                rejected.append(
                    Rejection(
                        account_id=None, reason="no_brief_submitted", details=[], stage="loop"
                    )
                )
            telemetry.duration_ms = int((time.monotonic() - started) * 1000)
            telemetry.estimated_cost_usd = config.estimate_cost_usd(
                telemetry.input_tokens, telemetry.output_tokens
            )
            return self._result(
                runtime, turns, rejected=rejected, telemetry=telemetry
            )

        brief, schema_rejection = guardrails.validate_brief(raw_brief)
        if schema_rejection:
            rejected.append(schema_rejection.model_copy(update={"stage": "schema"}))
            telemetry.duration_ms = int((time.monotonic() - started) * 1000)
            telemetry.estimated_cost_usd = config.estimate_cost_usd(
                telemetry.input_tokens, telemetry.output_tokens
            )
            return self._result(
                runtime, turns, rejected=rejected, telemetry=telemetry, raw_brief=raw_brief
            )

        published = []
        for index, rec in enumerate(brief.recommendations):
            if index >= config.MAX_RECOMMENDATIONS:
                rejected.append(
                    Rejection(
                        account_id=rec.account_id,
                        reason="recommendation_limit_exceeded",
                        details=[f"limit is {config.MAX_RECOMMENDATIONS} per run"],
                        stage="limit",
                        recommendation_index=index,
                        recommendation=rec.model_dump(mode="json"),
                    )
                )
                continue
            entry = by_account.get(rec.account_id)
            if entry is None:
                rejected.append(
                    Rejection(
                        account_id=rec.account_id,
                        reason="account_not_shortlisted",
                        details=["the deterministic scan never surfaced this account"],
                        stage="shortlist",
                        recommendation_index=index,
                        recommendation=rec.model_dump(mode="json"),
                    )
                )
                continue
            ok, rejection = guardrails.gate_recommendation(
                rec,
                facts=self.repo.account_facts(rec.account_id),
                available_source_refs=runtime.refs_for(rec.account_id),
                source_ref_provenance=runtime.source_ref_provenance,
                signal_severity=entry["top_severity"],
                competing_signals=entry["competing_signals"],
            )
            if ok:
                published.append(ok)
            else:
                rejected.append(
                    rejection.model_copy(
                        update={
                            "stage": "gate",
                            "recommendation_index": index,
                            "recommendation": rec.model_dump(mode="json"),
                        }
                    )
                )

        telemetry.accounts_recommended = len(published)
        telemetry.duration_ms = int((time.monotonic() - started) * 1000)
        telemetry.estimated_cost_usd = config.estimate_cost_usd(
            telemetry.input_tokens, telemetry.output_tokens
        )
        return self._result(
            runtime,
            turns,
            published=published,
            rejected=rejected,
            telemetry=telemetry,
            raw_brief=raw_brief,
        )

    def _result(self, runtime: ToolRuntime, turns: list[dict], **fields) -> RunResult:
        return RunResult(
            **fields,
            tool_log=runtime.tool_log,
            model_requested=self.model,
            turns=turns,
            available_source_refs=sorted(runtime.available_source_refs),
            source_ref_provenance=dict(sorted(runtime.source_ref_provenance.items())),
            shortlist_supplied=self._shortlist,
            system_prompt_sha256=_sha256(prompts.SYSTEM_PROMPT),
            tool_definitions_sha256=_sha256(json.dumps(TOOL_DEFINITIONS, sort_keys=True)),
        )


def _serialise(block: Any) -> dict:
    btype = getattr(block, "type", "text")
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": dict(block.input),
        }
    return {"type": "text", "text": getattr(block, "text", "")}
