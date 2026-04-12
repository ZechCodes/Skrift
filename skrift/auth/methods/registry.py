"""Registry for primary authentication methods."""

from __future__ import annotations

import importlib

from skrift.auth.methods.base import PrimaryAuthMethod
from skrift.auth.methods.dummy import DummyPrimaryAuthMethod
from skrift.auth.methods.oauth import OAuthPrimaryAuthMethod
from skrift.auth.methods.passkey import PasskeyPrimaryAuthMethod
from skrift.config import get_settings


_METHOD_CLASSES: dict[str, type[PrimaryAuthMethod]] = {
    "oauth": OAuthPrimaryAuthMethod,
    "dummy": DummyPrimaryAuthMethod,
    "passkey": PasskeyPrimaryAuthMethod,
}


def register_primary_auth_method(method_type: str, method_class: type[PrimaryAuthMethod]) -> None:
    """Register a primary-auth method class."""
    _METHOD_CLASSES[method_type] = method_class


def _import_method_class(dotted_path: str) -> type[PrimaryAuthMethod]:
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise ValueError(f"Invalid auth method path: {dotted_path}")

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, PrimaryAuthMethod)):
        raise TypeError(f"'{dotted_path}' must be a subclass of PrimaryAuthMethod")

    _METHOD_CLASSES[dotted_path] = cls
    return cls


def get_primary_auth_method(method_key: str) -> PrimaryAuthMethod:
    """Resolve a configured auth method instance by config key."""
    settings = get_settings()
    method_type = settings.auth.get_primary_auth_method_type(method_key)

    if "." in method_type and method_type not in _METHOD_CLASSES:
        cls = _import_method_class(method_type)
        return cls(method_key)

    cls = _METHOD_CLASSES.get(method_type)
    if cls is None:
        raise ValueError(f"Unknown auth method type: {method_type} (key: {method_key})")
    return cls(method_key)
