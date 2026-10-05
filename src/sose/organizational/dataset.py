from __future__ import annotations

from hashlib import sha256
import json
from math import ceil
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .observations import ObservedPRTrace


NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ObservedPRKey = tuple[str, int]


class ObservedPRDataset(BaseModel):
    """Hash-addressed observed PR traces with deterministic temporal ordering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_version: NonBlankString
    traces: tuple[ObservedPRTrace, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def canonicalize_and_validate(self) -> "ObservedPRDataset":
        keys = [(trace.repository, trace.pr_number) for trace in self.traces]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate observed pull request identity")
        ordered = tuple(
            sorted(
                self.traces,
                key=lambda trace: (trace.opened_at, trace.repository, trace.pr_number),
            )
        )
        object.__setattr__(self, "traces", ordered)
        return self

    @property
    def keys(self) -> tuple[ObservedPRKey, ...]:
        return tuple((trace.repository, trace.pr_number) for trace in self.traces)

    def terminal_only(self) -> "ObservedPRDataset":
        terminal = tuple(trace for trace in self.traces if trace.terminal_at is not None)
        if not terminal:
            raise ValueError("terminal-only dataset would be empty")
        return ObservedPRDataset(dataset_version=self.dataset_version, traces=terminal)

    def chronological_holdout(self, *, holdout_fraction: float) -> "ObservedPRSplit":
        if not 0.0 < holdout_fraction < 1.0:
            raise ValueError("holdout split requires 0 < holdout_fraction < 1")
        if len(self.traces) < 2:
            raise ValueError("chronological holdout requires at least two traces")
        if any(trace.terminal_at is None for trace in self.traces):
            raise ValueError("chronological holdout requires a terminal-only dataset")

        holdout_count = min(len(self.traces) - 1, max(1, ceil(len(self.traces) * holdout_fraction)))
        split_index = len(self.traces) - holdout_count
        holdout_traces = self.traces[split_index:]
        holdout_start = holdout_traces[0].opened_at

        candidate_train = self.traces[:split_index]
        train_traces = tuple(
            trace
            for trace in candidate_train
            if trace.terminal_at is not None and trace.terminal_at <= holdout_start
        )
        purged_keys = tuple(
            (trace.repository, trace.pr_number)
            for trace in candidate_train
            if trace.terminal_at is None or trace.terminal_at > holdout_start
        )
        if not train_traces:
            raise ValueError("purging leaves no training traces")

        train = ObservedPRDataset(dataset_version=self.dataset_version, traces=train_traces)
        holdout = ObservedPRDataset(dataset_version=self.dataset_version, traces=holdout_traces)
        return ObservedPRSplit(train=train, holdout=holdout, purged_keys=purged_keys)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "dataset_version": self.dataset_version,
            "traces": [trace.canonical_payload() for trace in self.traces],
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @property
    def dataset_hash(self) -> str:
        return sha256(self.canonical_json().encode("utf-8")).hexdigest()


class ObservedPRSplit(BaseModel):
    """Deterministic purged temporal train/holdout partition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    train: ObservedPRDataset
    holdout: ObservedPRDataset
    purged_keys: tuple[ObservedPRKey, ...] = ()

    @model_validator(mode="after")
    def validate_partition(self) -> "ObservedPRSplit":
        train_keys = set(self.train.keys)
        holdout_keys = set(self.holdout.keys)
        purged_keys = set(self.purged_keys)
        if train_keys & holdout_keys:
            raise ValueError("train and holdout datasets must not overlap")
        if train_keys & purged_keys or holdout_keys & purged_keys:
            raise ValueError("purged pull requests must not appear in train or holdout")
        if len(purged_keys) != len(self.purged_keys):
            raise ValueError("purged pull request identities must be unique")
        if self.train.traces[-1].opened_at > self.holdout.traces[0].opened_at:
            raise ValueError("chronological holdout must not precede training data")
        if any(
            trace.terminal_at is None or trace.terminal_at > self.holdout.traces[0].opened_at
            for trace in self.train.traces
        ):
            raise ValueError("training outcomes must be known before holdout begins")
        return self
