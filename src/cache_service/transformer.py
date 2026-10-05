from typing import Protocol


class Transformer(Protocol):
    def __call__(self, value: str) -> str: ...


def uppercase(value: str) -> str:
    """Stand-in for a deterministic external transformation service."""
    return value.upper()
