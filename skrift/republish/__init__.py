"""Optional cross-site republishing component."""

from skrift.auth.permissions import ALLOW_ANONYMOUS_SERVICE, ensure_permission

REPUBLISH_PERMISSION = "republish"


def register_republish_permission() -> None:
    """Make the republish API permission available to grant requests."""
    ensure_permission(
        REPUBLISH_PERMISSION,
        display_name="Republish content",
        description="Create, update, and remove reposted content from this service.",
        service_clearance=ALLOW_ANONYMOUS_SERVICE,
    )


register_republish_permission()

__all__ = ["REPUBLISH_PERMISSION", "register_republish_permission"]
