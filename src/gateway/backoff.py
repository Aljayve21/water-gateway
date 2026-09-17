"""Exponential backoff for the poll loop."""


class ExponentialBackoff:
    def __init__(self, initial_seconds: float = 1.0, max_seconds: float = 300.0, multiplier: float = 2.0) -> None:
        self.initial = initial_seconds
        self.max_seconds = max_seconds
        self.multiplier = multiplier
        self._attempts = 0

    def next(self) -> float:
        """Return the next delay and record a failure attempt."""
        delay = min(self.initial * (self.multiplier**self._attempts), self.max_seconds)
        self._attempts += 1
        return delay

    def reset(self) -> None:
        self._attempts = 0