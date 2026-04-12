"""Pluggable second-factor method interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SecondFactorMethodDescriptor:
    """Presentation metadata for a second-factor method."""

    key: str
    factor_type: str
    name: str
    verify_path: str
    is_available: bool = True
    availability_note: str = ""


class SecondFactorMethod(ABC):
    """Base class for pluggable second-factor methods."""

    factor_type: str

    def __init__(self, factor_key: str):
        self.factor_key = factor_key

    @abstractmethod
    def get_descriptor(self, settings) -> SecondFactorMethodDescriptor:
        """Return a descriptor used by the verification UI."""
        ...

