"""Second-factor authentication primitives."""

from skrift.auth.second_factors.base import SecondFactorMethod, SecondFactorMethodDescriptor
from skrift.auth.second_factors.registry import (
    get_second_factor_method,
    register_second_factor_method,
)

__all__ = [
    "SecondFactorMethod",
    "SecondFactorMethodDescriptor",
    "get_second_factor_method",
    "register_second_factor_method",
]
