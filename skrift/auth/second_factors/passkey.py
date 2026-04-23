"""Bundled passkey second-factor method."""

from __future__ import annotations

from skrift.auth.second_factors.base import SecondFactorMethod, SecondFactorMethodDescriptor
from skrift.auth.second_factors.passkey_service import is_webauthn_available


class PasskeySecondFactorMethod(SecondFactorMethod):
    """Built-in passkey second-factor method."""

    factor_type = "passkey"

    def get_descriptor(self, settings) -> SecondFactorMethodDescriptor:
        config = settings.auth.second_factors.get_method_config(self.factor_key)
        has_webauthn = is_webauthn_available()
        availability_note = ""
        if not has_webauthn:
            availability_note = "Install the optional WebAuthn runtime dependency to verify passkeys."

        return SecondFactorMethodDescriptor(
            key=self.factor_key,
            factor_type=self.factor_type,
            name=config.get("label", "") or "Passkey",
            verify_path=f"/auth/verify/{self.factor_key}",
            is_available=has_webauthn,
            availability_note=availability_note,
        )
