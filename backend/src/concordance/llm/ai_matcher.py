"""The grey-band adjudicator. The only place an LLM touches a decision.

It is worth being precise about how small that place is. The model never sees a
sanction record, never sees a provider row, never sees a name. It sees agreement
levels and weights for the two to five candidates the engine could not choose
between, and its answer is accepted only if it validates, names a supplied
candidate and cites supplied evidence. Everything above `t_auto_accept` and
below `t_auto_reject` was already decided by a calibrated statistical model
before this module was reached.

The mapping from the model's vocabulary onto the engine's is where the
conservative bias is enforced, and it is asymmetric on purpose:

- `MATCH` -> `Outcome.MATCH`, but only with a named candidate.
- `NO_CONFIDENT_MATCH` -> `Outcome.AMBIGUOUS`, **not** `NO_MATCH`. The model is
  saying "none of these convince me", which is exactly the condition a human
  reviewer exists for. Letting it close a record out of the queue would give an
  unverified model the power to silently drop a genuine sanction, and a missed
  sanction is the expensive error in this domain.
- `AMBIGUOUS` -> `Outcome.AMBIGUOUS`.

So the adjudicator can promote a grey-band record to a match, and can never
remove one from human review. Every failure mode - disabled, unconfigured, all
providers down, malformed output twice, invented evidence - lands on the same
abstention, and the record stays exactly where the unassisted engine left it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from concordance.config import Settings
from concordance.domain import Outcome
from concordance.llm.errors import LLMError
from concordance.llm.prompt import (
    PROMPT_VERSION,
    UnsafePromptValue,
    build_messages,
)
from concordance.llm.router import LLMRouter
from concordance.llm.schema import (
    ADJUDICATION_SCHEMA,
    DECISION_AMBIGUOUS,
    DECISION_MATCH,
    DECISION_NO_CONFIDENT_MATCH,
    Adjudication,
    ValidationResult,
    repair_instruction,
    validate,
)
from concordance.llm.types import ChatMessage, LLMResponse, assistant, user
from concordance.logging_setup import get_logger
from concordance.matching.adjudication import AdjudicationOutcome, AdjudicationRequest

log = get_logger("llm.adjudicator")

#: How the model's three answers map onto the engine's. See the module
#: docstring for why `NO_CONFIDENT_MATCH` does not become `NO_MATCH`.
DECISION_MAP = {
    DECISION_MATCH: Outcome.MATCH,
    DECISION_NO_CONFIDENT_MATCH: Outcome.AMBIGUOUS,
    DECISION_AMBIGUOUS: Outcome.AMBIGUOUS,
}


@dataclass
class AdjudicatorStats:
    calls: int = 0
    adjudicated: int = 0
    abstentions: int = 0
    repairs: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    matched: int = 0

    def reject(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1


@dataclass
class LlmAdjudicator:
    """Satisfies the Stage 0 `Adjudicator` protocol with a real model behind it.

    Substitutable for `NullAdjudicator` everywhere - the sweep, the eval
    harness and the CLI all take whichever they were handed and neither knows
    the difference beyond what `stats()` reports.
    """

    router: LLMRouter
    name: str = "llm"
    prompt_version: str = PROMPT_VERSION
    top_k: int = 3
    counters: AdjudicatorStats = field(default_factory=AdjudicatorStats)
    #: Cache key of the most recent call. Becomes `match_results.llm_call_id`
    #: at Stage 5; kept here so the caller need not reach into the router.
    last_call_key: str | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        router: LLMRouter | None = None,
        prompt_version: str | None = None,
    ) -> LlmAdjudicator:
        return cls(
            router=router or LLMRouter.from_settings(settings),
            prompt_version=prompt_version or PROMPT_VERSION,
            top_k=settings.LLM_TOP_K,
        )

    @property
    def available(self) -> bool:
        return self.router.available

    # -- the protocol -----------------------------------------------------
    def adjudicate(self, request: AdjudicationRequest) -> AdjudicationOutcome:
        self.counters.calls += 1
        if not self.router.available:
            return self._abstain(request, "adjudicator unavailable: LLM disabled or unconfigured")
        if not request.candidates:
            return self._abstain(request, "no candidates to adjudicate")

        try:
            messages = build_messages(request, self.prompt_version)
        except UnsafePromptValue as exc:
            # The request carried something that is not structured evidence.
            # Refusing to send it is the whole point of the check.
            log.error("llm.adjudicate.unsafe_payload", record_id=request.record_id, error=str(exc))
            return self._abstain(request, f"prompt refused: {exc}")

        allowed = {c.provider_id for c in request.candidates}
        supplied = request.supplied_evidence()

        try:
            response = self.router.complete(messages, ADJUDICATION_SCHEMA)
        except LLMError as exc:
            log.warning("llm.adjudicate.call_failed", record_id=request.record_id, error=str(exc))
            return self._abstain(request, f"adjudication call failed: {exc}")

        result = validate(
            response.content, allowed_provider_ids=allowed, supplied_evidence=supplied
        )
        if not result.ok:
            result, response = self._repair(messages, response, result, allowed, supplied, request)

        if not result.ok:
            reason = result.rejection or "unknown"
            self.counters.reject(reason)
            log.warning(
                "llm.adjudicate.rejected",
                record_id=request.record_id,
                rejection=reason,
                detail=result.detail,
            )
            return self._abstain(
                request,
                f"model response rejected ({reason}: {result.detail})",
                response=response,
            )

        assert result.adjudication is not None
        return self._accept(request, result.adjudication, response, repaired=result.repaired)

    def stats(self) -> dict[str, Any]:
        """`calls`, `tokens` and `cost_usd` at minimum - the protocol's contract."""
        router = self.router.stats_dict()
        return {
            "adjudicator": self.name,
            "calls": self.counters.calls,
            "adjudicated": self.counters.adjudicated,
            "abstentions": self.counters.abstentions,
            "matched": self.counters.matched,
            "repairs": self.counters.repairs,
            "rejections": dict(self.counters.rejections),
            "prompt_version": self.prompt_version,
            "tokens": router["tokens"],
            "cost_usd": router["cost_usd"],
            "router": router,
        }

    # -- internals --------------------------------------------------------
    def _repair(
        self,
        messages: list[ChatMessage],
        response: LLMResponse,
        failed: ValidationResult,
        allowed: set[str],
        supplied: frozenset[str],
        request: AdjudicationRequest,
    ) -> tuple[ValidationResult, LLMResponse]:
        """One repair round. One, deliberately - see `llm.schema`."""
        self.counters.repairs += 1
        log.info(
            "llm.adjudicate.repair",
            record_id=request.record_id,
            rejection=failed.rejection,
            detail=failed.detail,
        )
        repair_messages = [
            *messages,
            assistant(response.content),
            user(repair_instruction(failed, response.content)),
        ]
        try:
            repaired_response = self.router.complete(repair_messages, ADJUDICATION_SCHEMA)
        except LLMError as exc:
            log.warning("llm.adjudicate.repair_failed", record_id=request.record_id, error=str(exc))
            return failed, response

        result = validate(
            repaired_response.content,
            allowed_provider_ids=allowed,
            supplied_evidence=supplied,
        )
        result.repaired = True
        return result, repaired_response

    def _accept(
        self,
        request: AdjudicationRequest,
        adjudication: Adjudication,
        response: LLMResponse,
        *,
        repaired: bool,
    ) -> AdjudicationOutcome:
        decision = DECISION_MAP[adjudication.decision]
        self.counters.adjudicated += 1
        if decision is Outcome.MATCH:
            self.counters.matched += 1
        log.info(
            "llm.adjudicate.decided",
            record_id=request.record_id,
            decision=str(decision),
            model_decision=adjudication.decision,
            provider_id=adjudication.provider_id,
            confidence=round(adjudication.confidence, 4),
            cached=response.cached,
            repaired=repaired,
        )
        note = adjudication.reasoning
        if repaired:
            note = f"{note} [accepted after one repair round]".strip()
        return AdjudicationOutcome(
            decision=decision,
            provider_id=adjudication.provider_id,
            confidence=adjudication.confidence,
            evidence_cited=adjudication.evidence_cited,
            reasoning=note,
            abstained=False,
            prompt_version=self.prompt_version,
            tokens=response.total_tokens,
            cost_usd=response.cost_usd,
        )

    def _abstain(
        self,
        request: AdjudicationRequest,
        reason: str,
        response: LLMResponse | None = None,
    ) -> AdjudicationOutcome:
        """Every failure path ends here. The record stays where the engine left it."""
        self.counters.abstentions += 1
        return AdjudicationOutcome(
            decision=Outcome.AMBIGUOUS,
            provider_id=request.candidates[0].provider_id if request.candidates else None,
            abstained=True,
            reasoning=reason,
            prompt_version=self.prompt_version,
            tokens=response.total_tokens if response else 0,
            cost_usd=response.cost_usd if response else 0.0,
        )


def build_adjudicator(
    settings: Settings, *, router: LLMRouter | None = None, prompt_version: str | None = None
) -> Any:
    """The adjudicator this configuration calls for.

    `LLM_ENABLED=false` or no key returns `NullAdjudicator`, so a caller never
    has to branch on configuration: it asks for an adjudicator and gets one that
    abstains honestly, and the run reports which one it was.
    """
    from concordance.matching.adjudication import NullAdjudicator

    if not settings.LLM_ENABLED:
        log.info("llm.adjudicator.disabled", reason="LLM_ENABLED=false")
        return NullAdjudicator()
    adjudicator = LlmAdjudicator.from_settings(
        settings, router=router, prompt_version=prompt_version
    )
    if not adjudicator.available:
        log.warning("llm.adjudicator.unconfigured", chain=adjudicator.router.chain_names())
        return NullAdjudicator()
    return adjudicator


__all__ = ["DECISION_MAP", "AdjudicatorStats", "LlmAdjudicator", "build_adjudicator"]
