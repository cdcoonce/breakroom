from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


@dataclass
class RollLog:
    records: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        stream: str,
        tick: int,
        purpose: str,
        primitive: str,
        result: Any,
    ) -> None:
        self.records.append(
            {
                "stream": stream,
                "tick": tick,
                "purpose": purpose,
                "primitive": primitive,
                "result": result,
            }
        )


class RngStream:
    def __init__(
        self,
        *,
        seed: int,
        stream: str,
        tick: int,
        log: RollLog | None = None,
    ) -> None:
        self.seed = seed
        self.stream = stream
        self.tick = tick
        self.log = log
        self._rng = random.Random(_derive_seed(seed=seed, stream=stream, tick=tick))

    def uniform(self, purpose: str) -> float:
        result = self._rng.random()
        self._record(purpose=purpose, primitive="uniform", result=result)
        return result

    def bernoulli(self, purpose: str, *, probability: float) -> bool:
        if probability < 0 or probability > 1:
            raise ValueError("probability must be between 0 and 1")
        result = self._rng.random() < probability
        self._record(purpose=purpose, primitive="bernoulli", result=result)
        return result

    def weighted_choice(self, purpose: str, choices: Sequence[tuple[T, float]]) -> T:
        if not choices:
            raise ValueError("choices must not be empty")
        total = sum(weight for _, weight in choices)
        if total <= 0:
            raise ValueError("total choice weight must be positive")
        target = self._rng.random() * total
        running = 0.0
        result = choices[-1][0]
        for value, weight in choices:
            if weight < 0:
                raise ValueError("choice weights must be non-negative")
            running += weight
            if target < running:
                result = value
                break
        self._record(purpose=purpose, primitive="weighted_choice", result=result)
        return result

    def _record(self, *, purpose: str, primitive: str, result: Any) -> None:
        if self.log is not None:
            self.log.record(
                stream=self.stream,
                tick=self.tick,
                purpose=purpose,
                primitive=primitive,
                result=result,
            )


def _derive_seed(*, seed: int, stream: str, tick: int) -> int:
    digest = hashlib.sha256(f"{seed}:{stream}:{tick}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big")
