from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from enum import StrEnum

from .encoding import encode_deterministic_parts


def scoped_seed(root_seed: int, *scope: object) -> int:
    payload = encode_deterministic_parts(root_seed, *scope)
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


@dataclass(frozen=True, slots=True)
class RandomSource:
    """Legacy scoped mutable RNG factory retained for compatibility."""

    root_seed: int

    def for_scope(self, *scope: object) -> random.Random:
        return random.Random(scoped_seed(self.root_seed, *scope))


class RNGMode(StrEnum):
    LEGACY = "legacy"
    COUNTER = "counter"


@dataclass(frozen=True, slots=True)
class CounterRandomSource:
    """Stateless random draws keyed by logical stochastic identity."""

    root_seed: int

    def _counter(
        self,
        *,
        stream: str,
        entity_id: object,
        mechanism: str,
        draw_index: int,
    ) -> int:
        if draw_index < 0:
            raise ValueError("draw_index must be non-negative")
        return scoped_seed(
            self.root_seed,
            "counter-v1",
            stream,
            entity_id,
            mechanism,
            draw_index,
        )

    def uniform(
        self,
        *,
        stream: str,
        entity_id: object,
        mechanism: str,
        draw_index: int = 0,
    ) -> float:
        """Return a deterministic U[0, 1) draw for one logical mechanism."""
        value = self._counter(
            stream=stream,
            entity_id=entity_id,
            mechanism=mechanism,
            draw_index=draw_index,
        )
        return (value >> 11) / float(1 << 53)

    def bernoulli(
        self,
        probability: float,
        *,
        stream: str,
        entity_id: object,
        mechanism: str,
        draw_index: int = 0,
    ) -> bool:
        """Threshold one stable latent uniform draw for scenario coherence."""
        if not 0.0 <= probability <= 1.0:
            raise ValueError("probability must be between 0 and 1")
        return (
            self.uniform(
                stream=stream,
                entity_id=entity_id,
                mechanism=mechanism,
                draw_index=draw_index,
            )
            < probability
        )


def make_random_source(
    root_seed: int,
    *,
    mode: RNGMode | str = RNGMode.LEGACY,
) -> RandomSource | CounterRandomSource:
    resolved = RNGMode(mode)
    if resolved is RNGMode.LEGACY:
        return RandomSource(root_seed)
    return CounterRandomSource(root_seed)
