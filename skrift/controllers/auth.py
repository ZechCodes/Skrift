import secrets
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from typing import Annotated

from litestar import Controller, Request, get
from litestar.params import Parameter
from litestar.exceptions import HTTPException
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.config import get_settings
from skrift.db.models.role import Role
from skrift.db.models.user import User

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


class AuthController(Controller):
    path = "/auth"

    @get("/google/login")
    async def google_login(self, request: Request) -> Redirect:
        """Redirect to Google OAuth consent screen."""
        settings = get_settings()
        google_config = settings.auth.providers["google"]

        # Generate CSRF state token
        state = secrets.token_urlsafe(32)
        request.session["oauth_state"] = state

        params = {
            "client_id": google_config.client_id,
            "redirect_uri": settings.auth.get_redirect_uri("google"),
            "response_type": "code",
            "scope": " ".join(google_config.scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "select_account",
        }

        auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
        return Redirect(path=auth_url)

    @get("/google/callback")
    async def google_callback(
        self,
        request: Request,
        db_session: AsyncSession,
        code: str | None = None,
        oauth_state: Annotated[str | None, Parameter(query="state")] = None,
        error: str | None = None,
    ) -> Redirect:
        """Handle Google OAuth callback."""
        settings = get_settings()
        google_config = settings.auth.providers["google"]

        # Check for OAuth errors
        if error:
            request.session["flash"] = f"OAuth error: {error}"
            return Redirect(path="/auth/login")

        # Verify CSRF state
        stored_state = request.session.pop("oauth_state", None)
        if not oauth_state or oauth_state != stored_state:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        if not code:
            raise HTTPException(status_code=400, detail="Missing authorization code")

        # Exchange code for tokens
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": google_config.client_id,
                    "client_secret": google_config.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.auth.get_redirect_uri("google"),
                },
            )

            if token_response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to exchange code for tokens")

            tokens = token_response.json()
            access_token = tokens.get("access_token")

            # Fetch user info
            userinfo_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )

            if userinfo_response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")

            userinfo = userinfo_response.json()

        # Find or create user
        oauth_id = userinfo.get("id")
        email = userinfo.get("email")

        result = await db_session.execute(select(User).where(User.oauth_id == oauth_id))
        user = result.scalar_one_or_none()

        if user:
            # Update existing user
            user.name = userinfo.get("name")
            user.picture_url = userinfo.get("picture")
            user.last_login_at = datetime.now(UTC)
        else:
            # Check if this will be the first user
            user_count = await db_session.scalar(select(func.count()).select_from(User))
            is_first_user = user_count == 0

            # Create new user
            user = User(
                oauth_provider="google",
                oauth_id=oauth_id,
                email=email,
                name=userinfo.get("name"),
                picture_url=userinfo.get("picture"),
                last_login_at=datetime.now(UTC),
            )
            db_session.add(user)
            await db_session.flush()  # Get the user ID

            # Assign admin role to the first user
            if is_first_user:
                admin_role = await db_session.scalar(
                    select(Role).where(Role.name == "admin")
                )
                if admin_role:
                    user.roles.append(admin_role)

        await db_session.commit()

        # Set session
        request.session["user_id"] = str(user.id)
        request.session["flash"] = "Successfully logged in!"

        return Redirect(path="/")

    @get("/login")
    async def login_page(self, request: Request) -> TemplateResponse:
        """Show login page."""
        flash = request.session.pop("flash", None)
        return TemplateResponse("auth/login.html", context={"flash": flash})

    @get("/logout")
    async def logout(self, request: Request) -> Redirect:
        """Clear session and redirect to home."""
        request.session.clear()
        return Redirect(path="/")
