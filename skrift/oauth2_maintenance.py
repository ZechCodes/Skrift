"""Background maintenance for the OAuth2 Authorization Server.

Currently hosts the Dynamic Client Registration pruner: dynamically-registered
clients that were created but never used are deleted once they age past the
configured window. The pruning logic itself lives in
:func:`skrift.db.services.oauth2_service.prune_stale_dynamic_clients` so it can
be unit-tested directly; this module only supplies the worker handler and its
process-local database access, mirroring ``skrift.webhooks``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import skrift
from skrift.config import get_settings
from skrift.db.services import oauth2_service

_session_maker: Any | None = None

PRUNE_DYNAMIC_CLIENTS_JOB = "oauth2.prune_dynamic_clients"


class OAuth2MaintenanceConfigurationError(RuntimeError):
    """Raised when the maintenance job runs without a configured session maker."""


def configure_oauth2_maintenance(*, session_maker: Any) -> None:
    """Configure process-local database access for the maintenance handler."""
    global _session_maker
    _session_maker = session_maker


def _require_session_maker() -> Any:
    if _session_maker is None:
        raise OAuth2MaintenanceConfigurationError(
            "OAuth2 maintenance jobs have no session maker configured"
        )
    return _session_maker


class PruneDynamicClients(skrift.Job):
    """Worker payload for the dynamic-client pruning sweep.

    ``max_age_days`` overrides the configured retention window when set; leave
    it unset to use ``settings.oauth2_dynamic_client_max_age_days``.
    """

    max_age_days: int | None = None


@skrift.handler(PRUNE_DYNAMIC_CLIENTS_JOB, queue="default", max_attempts=1)
async def prune_dynamic_clients(job: PruneDynamicClients, context) -> dict[str, Any]:
    """Delete unused, aged-out dynamically-registered OAuth2 clients."""
    settings = get_settings()
    max_age_days = job.max_age_days if job.max_age_days is not None else settings.oauth2_dynamic_client_max_age_days
    session_maker = _require_session_maker()
    async with session_maker() as db_session:
        deleted = await oauth2_service.prune_stale_dynamic_clients(
            db_session,
            now=datetime.now(tz=timezone.utc),
            max_age_days=max_age_days,
        )
    return {"deleted": deleted}
