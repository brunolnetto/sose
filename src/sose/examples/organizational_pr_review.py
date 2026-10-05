from __future__ import annotations

from heapq import heappop, heappush
from math import log1p
from statistics import fmean
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sose.core.randomness import CounterRandomSource
from sose.organizational.ledger import ActorCategory, ActorLedger, ItemCategory, ItemLedger
from sose.organizational.model_spec import EvidenceClass, ModelSpec
from sose.organizational.projection import AccountingEvent, LedgerProjector


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PullRequestFlowConfig(BaseModel):
    """Minimal A0 configuration for well-specified pull-request review work."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_count: int = Field(default=1, ge=1)
    ci_time: float = Field(default=1.0, gt=0.0, allow_inf_nan=False)
    mean_review_time: float = Field(default=2.0, gt=0.0, allow_inf_nan=False)
    mean_revision_time: float = Field(default=1.0, gt=0.0, allow_inf_nan=False)
    rework_probability: float = Field(default=0.2, ge=0.0, lt=1.0, allow_inf_nan=False)


class PullRequestFlowEvidence(BaseModel):
    """Evidence provenance for canonical configuration parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewer_count: EvidenceClass = EvidenceClass.OBSERVED
    ci_time: EvidenceClass = EvidenceClass.OBSERVED
    mean_review_time: EvidenceClass = EvidenceClass.ASSUMED
    mean_revision_time: EvidenceClass = EvidenceClass.ASSUMED
    rework_probability: EvidenceClass = EvidenceClass.INFERABLE


class PullRequestCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_id: NonBlankString
    opened_at: float = Field(ge=0.0, allow_inf_nan=False)


class PullRequestFlowResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_spec_hash: str
    opened_at: dict[str, float]
    completed_at: dict[str, float]
    review_cycles: dict[str, int]
    reviewer_ids: tuple[str, ...]
    events: tuple[AccountingEvent, ...]

    def _projector(self) -> LedgerProjector:
        projector = LedgerProjector()
        for event in self.events:
            projector.apply(event)
        return projector

    def item_ledger(self, pr_id: str) -> ItemLedger:
        return self._projector().item_ledger(pr_id)

    def actor_ledger(self, actor_id: str) -> ActorLedger:
        return self._projector().actor_ledger(actor_id)

    @property
    def mean_lead_time(self) -> float:
        return fmean(
            self.completed_at[pr_id] - opened
            for pr_id, opened in self.opened_at.items()
        )

    @property
    def throughput(self) -> float:
        observation_start = min(self.opened_at.values())
        observation_end = max(self.completed_at.values())
        return len(self.completed_at) / (observation_end - observation_start)

    @property
    def peak_wip(self) -> int:
        boundaries = [
            *((opened, 1) for opened in self.opened_at.values()),
            *((completed, -1) for completed in self.completed_at.values()),
        ]
        current = 0
        peak = 0
        for _, delta in sorted(boundaries, key=lambda entry: (entry[0], entry[1])):
            current += delta
            peak = max(peak, current)
        return peak


def build_model_spec(
    config: PullRequestFlowConfig,
    *,
    evidence: PullRequestFlowEvidence | None = None,
) -> ModelSpec:
    evidence = evidence or PullRequestFlowEvidence()
    reviewers = {
        f"reviewer-{index}": {"capabilities": ["review"]}
        for index in range(config.reviewer_count)
    }
    parameters = {
        "reviewer_count": config.reviewer_count,
        "ci_time": config.ci_time,
        "mean_review_time": config.mean_review_time,
        "mean_revision_time": config.mean_revision_time,
        "rework_probability": config.rework_probability,
    }
    return ModelSpec(
        stations={
            "ci": {"kind": "automation", "capacity": "uncapped-delay"},
            "review": {"kind": "human-review", "capacity": config.reviewer_count},
            "revision": {"kind": "author-rework", "capacity": "per-item"},
        },
        actors=reviewers,
        capabilities={"review": {"station": "review"}},
        routing={
            "initial": ["ci", "review"],
            "review_rework": ["revision", "review"],
            "review_pass": ["complete"],
        },
        quality_gates={
            "review": {
                "rework_probability": config.rework_probability,
                "mechanism": "review_outcome",
            }
        },
        policies={"review_dispatch": "fcfs"},
        parameters=parameters,
        parameter_evidence={
            "reviewer_count": evidence.reviewer_count,
            "ci_time": evidence.ci_time,
            "mean_review_time": evidence.mean_review_time,
            "mean_revision_time": evidence.mean_revision_time,
            "rework_probability": evidence.rework_probability,
        },
    )


def simulate_pull_request_flow(
    *,
    cases: tuple[PullRequestCase, ...],
    config: PullRequestFlowConfig,
    seed: int,
    evidence: PullRequestFlowEvidence | None = None,
) -> PullRequestFlowResult:
    if not cases:
        raise ValueError("at least one pull request is required")
    ids = [case.pr_id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate pull request ids are not allowed")

    spec = build_model_spec(config, evidence=evidence)
    spec_hash = spec.model_spec_hash
    rng = CounterRandomSource(seed)
    events: list[AccountingEvent] = []
    completed_at: dict[str, float] = {}
    review_cycles: dict[str, int] = {}
    opened_at = {case.pr_id: case.opened_at for case in cases}
    reviewer_ids = tuple(f"reviewer-{index}" for index in range(config.reviewer_count))
    reviewer_available = {reviewer_id: 0.0 for reviewer_id in reviewer_ids}
    ready_queue: list[tuple[float, str, int]] = []

    for case in sorted(cases, key=lambda item: (item.opened_at, item.pr_id)):
        ci_end = case.opened_at + config.ci_time
        events.append(
            AccountingEvent.item(
                event_id=f"{case.pr_id}:ci",
                work_item_id=case.pr_id,
                start=case.opened_at,
                end=ci_end,
                category=ItemCategory.PROCESSING,
                secondary_labels=("ci",),
                model_spec_hash=spec_hash,
            )
        )
        heappush(ready_queue, (ci_end, case.pr_id, 0))

    while ready_queue:
        ready_at, pr_id, cycle = heappop(ready_queue)
        reviewer_id = min(reviewer_ids, key=lambda actor: (reviewer_available[actor], actor))
        review_start = max(ready_at, reviewer_available[reviewer_id])
        if review_start > ready_at:
            events.append(
                AccountingEvent.item(
                    event_id=f"{pr_id}:queue:{cycle}",
                    work_item_id=pr_id,
                    start=ready_at,
                    end=review_start,
                    category=ItemCategory.QUEUE,
                    secondary_labels=("review",),
                    model_spec_hash=spec_hash,
                )
            )

        review_duration = _exponential_requirement(
            rng,
            mean=config.mean_review_time,
            stream="service_time",
            entity_id=pr_id,
            mechanism="review_service",
            draw_index=cycle,
        )
        review_end = review_start + review_duration
        events.extend(
            (
                AccountingEvent.item(
                    event_id=f"{pr_id}:review:item:{cycle}",
                    work_item_id=pr_id,
                    start=review_start,
                    end=review_end,
                    category=ItemCategory.COORDINATION,
                    secondary_labels=("review",),
                    model_spec_hash=spec_hash,
                ),
                AccountingEvent.actor(
                    event_id=f"{pr_id}:review:actor:{cycle}",
                    actor_id=reviewer_id,
                    start=review_start,
                    end=review_end,
                    category=ActorCategory.COORDINATION,
                    work_item_id=pr_id,
                    secondary_labels=("review",),
                    model_spec_hash=spec_hash,
                ),
            )
        )
        reviewer_available[reviewer_id] = review_end

        requires_rework = rng.bernoulli(
            config.rework_probability,
            stream="rework",
            entity_id=pr_id,
            mechanism="review_outcome",
            draw_index=cycle,
        )
        if not requires_rework:
            completed_at[pr_id] = review_end
            review_cycles[pr_id] = cycle + 1
            continue

        revision_duration = _exponential_requirement(
            rng,
            mean=config.mean_revision_time,
            stream="service_time",
            entity_id=pr_id,
            mechanism="revision_service",
            draw_index=cycle,
        )
        revision_end = review_end + revision_duration
        events.extend(
            (
                AccountingEvent.item(
                    event_id=f"{pr_id}:rework:item:{cycle}",
                    work_item_id=pr_id,
                    start=review_end,
                    end=revision_end,
                    category=ItemCategory.REWORK,
                    secondary_labels=("review_rework",),
                    model_spec_hash=spec_hash,
                ),
                AccountingEvent.actor(
                    event_id=f"{pr_id}:rework:actor:{cycle}",
                    actor_id=f"author:{pr_id}",
                    start=review_end,
                    end=revision_end,
                    category=ActorCategory.EXECUTION,
                    work_item_id=pr_id,
                    secondary_labels=("rework",),
                    model_spec_hash=spec_hash,
                ),
            )
        )
        heappush(ready_queue, (revision_end, pr_id, cycle + 1))

    return PullRequestFlowResult(
        model_spec_hash=spec_hash,
        opened_at=opened_at,
        completed_at=completed_at,
        review_cycles=review_cycles,
        reviewer_ids=reviewer_ids,
        events=tuple(sorted(events, key=lambda event: (event.start, event.end, event.event_id))),
    )


def _exponential_requirement(
    rng: CounterRandomSource,
    *,
    mean: float,
    stream: str,
    entity_id: str,
    mechanism: str,
    draw_index: int,
) -> float:
    uniform = rng.uniform(
        stream=stream,
        entity_id=entity_id,
        mechanism=mechanism,
        draw_index=draw_index,
    )
    positive_uniform = max(uniform, 1.0 / (1 << 53))
    return -log1p(-positive_uniform) * mean
