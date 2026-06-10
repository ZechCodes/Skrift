"""Provider Skrift site for the API permission grant demo."""

from __future__ import annotations

from litestar import Controller, Request, get
from litestar.response import Response
from litestar.response import Template as TemplateResponse

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard
from skrift.auth.permissions import (
    ALLOW_ANONYMOUS_SERVICE,
    DISALLOW_API_GRANTS,
    REQUIRE_ELEVATED_SECURITY,
    REQUIRE_KNOWN_SERVICE,
    register_permission,
)
from skrift.auth.roles import register_role
from skrift.hooks import AFTER_USER_CREATED, action

from apigrantsdemo.permissions import (
    DEMO_ROLE,
    PERM_ANONYMOUS,
    PERM_DISALLOWED,
    PERM_ELEVATED,
    PERM_KNOWN,
)


register_permission(
    PERM_ANONYMOUS,
    display_name="Read Public Demo Data",
    description="Read a harmless provider API response that is safe for unknown services.",
    service_clearance=ALLOW_ANONYMOUS_SERVICE,
)
register_permission(
    PERM_KNOWN,
    display_name="Read Partner Demo Data",
    description="Read provider API data only available to known services.",
    service_clearance=REQUIRE_KNOWN_SERVICE,
)
register_permission(
    PERM_ELEVATED,
    display_name="Write Elevated Demo Data",
    description="Perform a sensitive provider API action after user consent.",
    service_clearance=REQUIRE_ELEVATED_SECURITY,
)
register_permission(
    PERM_DISALLOWED,
    display_name="Administer Demo Provider",
    description="A deliberately blocked permission that cannot be granted through the flow.",
    service_clearance=DISALLOW_API_GRANTS,
)

register_role(
    DEMO_ROLE,
    PERM_ANONYMOUS,
    PERM_KNOWN,
    PERM_ELEVATED,
    PERM_DISALLOWED,
    display_name="API Grant Demo User",
    description="Demo role containing all provider API permissions.",
)


@action(AFTER_USER_CREATED)
async def assign_demo_role(login_result, request) -> None:
    """Give dummy-login users the demo API permissions."""

    from skrift.auth.services import assign_role_to_user

    session_maker = request.app.state.session_maker_class
    async with session_maker() as session:
        await assign_role_to_user(session, login_result.user.id, DEMO_ROLE)


class ProviderDemoController(Controller):
    """Provider site that grants and serves protected API endpoints."""

    path = "/"

    @get("/")
    async def index(self, request: Request) -> TemplateResponse:
        return TemplateResponse(
            "api-grants-demo/provider.html",
            context={"request": request},
        )

    @get("/favicon.ico")
    async def favicon(self) -> Response[bytes]:
        return Response(content=b"", status_code=204)

    @get("/api/demo/anonymous", guards=[auth_guard, APIKeyOnly(), Permission(PERM_ANONYMOUS)])
    async def anonymous_api(self) -> dict:
        return {
            "permission": PERM_ANONYMOUS,
            "message": "Anonymous-service grant API call succeeded.",
        }

    @get("/api/demo/known", guards=[auth_guard, APIKeyOnly(), Permission(PERM_KNOWN)])
    async def known_api(self) -> dict:
        return {
            "permission": PERM_KNOWN,
            "message": "Known-service grant API call succeeded.",
        }

    @get("/api/demo/elevated", guards=[auth_guard, APIKeyOnly(), Permission(PERM_ELEVATED)])
    async def elevated_api(self) -> dict:
        return {
            "permission": PERM_ELEVATED,
            "message": "Elevated-security grant API call succeeded.",
        }

    @get("/api/demo/disallowed", guards=[auth_guard, APIKeyOnly(), Permission(PERM_DISALLOWED)])
    async def disallowed_api(self) -> dict:
        return {
            "permission": PERM_DISALLOWED,
            "message": "This route exists, but the permission is not grantable by third parties.",
        }
