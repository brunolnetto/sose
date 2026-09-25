from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass


def scoped_seed(root_seed: int, *scope: object) -> int:
    payload = "|".join([str(root_seed), *(str(x) for x in scope)]).encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=False)


@dataclass(frozen=True, slots=True)
class RandomSource:
    root_seed: int

    def for_scope(self, *scope: object) -> random.Random:
        return random.Random(scoped_seed(self.root_seed, *scope))
