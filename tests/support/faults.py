from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field


class InjectedCrash(RuntimeError):
    """Synthetic process crash raised at a named deterministic checkpoint."""

    def __init__(self, checkpoint: str) -> None:
        super().__init__(f"injected crash at {checkpoint}")
        self.checkpoint = checkpoint


@dataclass
class FaultInjector:
    """Deterministic one-shot/multi-shot crash injector for tests."""

    armed: Counter[str] = field(default_factory=Counter)

    def arm(self, checkpoint: str, *, occurrence: int = 1) -> None:
        if occurrence < 1:
            raise ValueError("occurrence must be >= 1")
        self.armed[checkpoint] = occurrence

    def hit(self, checkpoint: str) -> None:
        remaining = self.armed.get(checkpoint, 0)
        if remaining <= 0:
            return
        if remaining == 1:
            del self.armed[checkpoint]
            raise InjectedCrash(checkpoint)
        self.armed[checkpoint] = remaining - 1

    def before(self, checkpoint: str, callback: Callable):
        def wrapped(*args, **kwargs):
            self.hit(checkpoint)
            return callback(*args, **kwargs)

        return wrapped

    def after(self, checkpoint: str, callback: Callable):
        def wrapped(*args, **kwargs):
            result = callback(*args, **kwargs)
            self.hit(checkpoint)
            return result

        return wrapped
