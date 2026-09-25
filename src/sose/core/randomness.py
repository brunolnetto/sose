from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from .encoding import encode_deterministic_parts


def scoped_seed(root_seed: int, *scope: object) -> int:
    payload = encode_deterministic_parts(root_seed, *scope)
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


@dataclass(frozen=True, slots=True)
class RandomSource:
    root_seed: int

    def for_scope(self, *scope: object) -> random.Random:
        return random.Random(scoped_seed(self.root_seed, *scope))
