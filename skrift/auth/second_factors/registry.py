"""Registry for second-factor methods."""

from __future__ import annotations

import importlib

from skrift.auth.second_factors.base import SecondFactorMethod
from skrift.auth.second_factors.passkey import PasskeySecondFactorMethod
from skrift.config import get_settings


_SECOND_FACTOR_CLASSES: dict[str, type[SecondFactorMethod]] = {
    "passkey": PasskeySecondFactorMethod,
}


def register_second_factor_method(factor_type: str, method_class: type[SecondFactorMethod]) -> None:
    """Register a second-factor method class."""
    _SECOND_FACTOR_CLASSES[factor_type] = method_class


def _import_method_class(dotted_path: str) -> type[SecondFactorMethod]:
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        raise ValueError(f"Invalid second factor method path: {dotted_path}")

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not (isinstance(cls, type) and issubclass(cls, SecondFactorMethod)):
        raise TypeError(f"'{dotted_path}' must be a subclass of SecondFactorMethod")

    _SECOND_FACTOR_CLASSES[dotted_path] = cls
    return cls


def get_second_factor_method(factor_key: str) -> SecondFactorMethod:
    """Resolve a configured second-factor method instance by config key."""
    settings = get_settings()
    factor_type = settings.auth.second_factors.get_method_type(factor_key)

    if "." in factor_type and factor_type not in _SECOND_FACTOR_CLASSES:
        cls = _import_method_class(factor_type)
        return cls(factor_key)

    cls = _SECOND_FACTOR_CLASSES.get(factor_type)
    if cls is None:
        raise ValueError(f"Unknown second factor type: {factor_type} (key: {factor_key})")
    return cls(factor_key)
