"""Primary authentication method primitives."""

from skrift.auth.methods.base import (
    PrimaryAuthCompletion,
    PrimaryAuthMethod,
    PrimaryAuthMethodDescriptor,
)
from skrift.auth.methods.registry import get_primary_auth_method, register_primary_auth_method

__all__ = [
    "PrimaryAuthCompletion",
    "PrimaryAuthMethod",
    "PrimaryAuthMethodDescriptor",
    "get_primary_auth_method",
    "register_primary_auth_method",
]
