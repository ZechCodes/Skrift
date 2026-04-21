"""Authentication controller for OAuth login flows.

Supports multiple OAuth providers: Google, GitHub, Microsoft, Discord, Facebook, X (Twitter).
Also supports a development-only "dummy" provider for testing.
"""

import fnmatch
import hashlib
import json
import logging
from typing import Annotated
from urllib.parse import urlparse
from uuid import UUID
from litestar import Controller, Request, get, post
from litestar.exceptions import HTTPException, NotFoundException
from litestar.params import Parameter
from litestar.response import Redirect, Response, Template as TemplateResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.email_link import (
    begin_email_link_challenge,
    build_claim_url,
    clear_pending_link_state,
    expiry_from_payload,
    get_pending_link_masked_email,
    pop_pending_link_metadata,
    pop_pending_link_tokens,
    verify_link_token,
)
from skrift.auth.identities import ResolvedPrimaryIdentity
from skrift.auth.methods import get_primary_auth_method
from skrift.auth.oauth_account_service import (
    EmailVerificationRequired,
    LoginResult,
    complete_verified_email_link,
    create_login_result_for_new_user,
    find_login_result_for_passkey_credential,
    find_or_create_user_for_identity,
)
from skrift.auth.oauth_flow import exchange_and_fetch_oauth_identity
from skrift.auth.providers import NormalizedUserData
from skrift.auth.second_factors.passkey_service import (
    PasskeyRuntimeUnavailableError,
    PasskeyStateError,
    PasskeyVerificationError,
    begin_primary_passkey_authentication,
    begin_primary_passkey_registration,
    begin_passkey_authentication as begin_passkey_authentication_flow,
    begin_passkey_registration as begin_passkey_registration_flow,
    complete_primary_passkey_authentication,
    complete_primary_passkey_registration,
    complete_passkey_authentication as complete_passkey_authentication_flow,
    complete_passkey_registration as complete_passkey_registration_flow,
    get_primary_passkey_registration_state,
)
from skrift.auth.second_factors.registry import get_second_factor_method
from skrift.auth.second_factors.services import (
    build_second_factor_transition_decision,
    get_second_factor_enrollment_by_credential_id,
    list_available_second_factor_descriptors,
    list_second_factor_enrollments_for_factor,
    save_passkey_enrollment,
    touch_second_factor_enrollment,
)
from skrift.auth.session_service import (
    apply_pending_authentication_transition,
    begin_pending_authentication,
    clear_pending_authentication,
    complete_pending_authentication,
    finalize_authenticated_session,
    get_pending_authentication,
)
from skrift.db.models.user import User
from skrift.auth.session_keys import (
    SESSION_AUTH_NEXT,
    SESSION_USER_ID,
)
from jinja2.exceptions import TemplatesNotFound

from skrift.forms import verify_csrf
from skrift.forms.core import CSRF_SESSION_KEY
from skrift.lib.flash import flash_error, flash_success, get_flash_messages
from skrift.lib.hooks import hooks
from skrift.lib.template import resolve_template_name
from skrift.config import get_settings
from skrift.setup.providers import DUMMY_PROVIDER_KEY

logger = logging.getLogger(__name__)


async def _build_post_login_redirect(request: Request, settings, login_result: LoginResult) -> Redirect:
    """Run post-login hooks and return the final redirect target."""
    await hooks.do_action("after_login", login_result, request)
    if login_result.is_new_user:
        await hooks.do_action("after_user_created", login_result, request)

    next_url = _get_safe_redirect_url(request, settings.auth.allowed_redirect_domains)
    next_url = await hooks.apply_filters("login_redirect", next_url, login_result, request)
    return Redirect(path=next_url)


async def _complete_pending_login(
    request: Request,
    settings,
    user: User,
    pending_auth,
) -> Redirect:
    """Promote pending auth and resume the normal post-login hook/redirect flow."""
    complete_pending_authentication(request, user, pending_auth=pending_auth)
    return await _build_post_login_redirect(
        request,
        settings,
        LoginResult(
            user=user,
            identity_record=None,
            is_new_user=pending_auth.is_new_user,
            method_key=pending_auth.method_key,
            method_type=pending_auth.method_type,
        ),
    )


async def _get_authenticated_user(request: Request, db_session: AsyncSession) -> User | None:
    """Load the currently authenticated user from the session."""
    user_id = request.session.get(SESSION_USER_ID)
    if not user_id:
        return None

    result = await db_session.execute(select(User).where(User.id == UUID(str(user_id))))
    return result.scalar_one_or_none()


def _csrf_error(request: Request, error: str, *, status_code: int = 400) -> Response:
    """JSON error that always includes the current CSRF token.

    ``verify_csrf`` rotates the session token on success, so error responses
    emitted *after* CSRF validation must return the new token — otherwise the
    client's hidden ``_csrf`` field goes stale and the next submit fails with
    ``invalid_csrf``.
    """
    return Response(
        content={
            "error": error,
            "csrf_token": request.session.get(CSRF_SESSION_KEY, ""),
        },
        status_code=status_code,
    )


async def _create_primary_passkey_signup_login(
    db_session: AsyncSession,
    *,
    method_key: str,
    factor_key: str,
    email: str,
    name: str | None,
    registration_result,
) -> LoginResult:
    """Create a new user plus passkey enrollment for primary passkey signup."""
    existing = await db_session.execute(select(User).where(User.email == email))
    existing_user = existing.scalar_one_or_none()
    if existing_user is not None:
        raise ValueError("An account with that email already exists")

    login_result = await create_login_result_for_new_user(
        db_session,
        email=email,
        name=name,
        picture_url=None,
        method_key=method_key,
        method_type="passkey",
    )
    await save_passkey_enrollment(
        db_session,
        user_id=str(login_result.user.id),
        factor_key=factor_key,
        display_name=name,
        credential_id=registration_result.credential_id,
        public_key=registration_result.public_key,
        sign_count=registration_result.sign_count,
        transports=registration_result.transports,
        enrollment_metadata=registration_result.enrollment_metadata,
    )
    return login_result


def _get_default_passkey_factor_key(settings) -> str | None:
    """Return the first configured passkey factor key, if any."""
    for key in settings.auth.second_factors.get_method_keys():
        if settings.auth.second_factors.get_method_type(key) == "passkey":
            return key
    return None


def _is_passkey_factor(settings, factor_key: str) -> bool:
    """Return True when the factor key is configured as a passkey method."""
    return settings.auth.second_factors.get_method_type(factor_key) == "passkey"


def _get_passkey_factor_key_for_method(settings, method_key: str) -> str:
    """Resolve the passkey enrollment key to use for a primary passkey method."""
    config = settings.auth.get_method_config(method_key)
    factor_key = config.get("factor_key", "") or method_key
    return str(factor_key)


async def _finalize_primary_login(
    request: Request,
    db_session: AsyncSession,
    settings,
    login_result,
    *,
    identity: ResolvedPrimaryIdentity,
) -> Redirect:
    """Apply pending-auth transition policy and produce the post-auth redirect."""
    pending_auth = begin_pending_authentication(
        request,
        method_key=login_result.method_key,
        method_type=login_result.method_type,
        identity=identity,
        user_id=str(login_result.user.id),
        is_new_user=login_result.is_new_user,
    )
    initial_decision = await build_second_factor_transition_decision(
        db_session,
        settings,
        login_result,
        pending_auth,
    )
    decision = await apply_pending_authentication_transition(
        request,
        login_result.user,
        login_result=login_result,
        pending_auth=pending_auth,
        initial_decision=initial_decision,
    )

    if not decision.promote_immediately:
        return Redirect(path=decision.next_url)

    return await _build_post_login_redirect(request, settings, login_result)


def _is_safe_redirect_url(url: str, allowed_domains: list[str]) -> bool:
    """Check if URL is safe to redirect to.

    Supports wildcard patterns using fnmatch-style matching:
    - "*.example.com" matches any subdomain of example.com
    - "app-*.example.com" matches app-foo.example.com, app-bar.example.com, etc.
    - "example.com" (no wildcards) matches example.com and all subdomains
    """
    # Relative paths are always safe (but not protocol-relative //domain.com)
    if url.startswith("/") and not url.startswith("//"):
        return True

    # Parse absolute URL
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    # Must have scheme and netloc
    if not parsed.scheme or not parsed.netloc:
        return False

    # Only allow http/https
    if parsed.scheme not in ("http", "https"):
        return False

    # Check if domain matches allowed list
    host = parsed.netloc.lower().split(":")[0]  # Remove port
    for pattern in allowed_domains:
        pattern = pattern.lower()
        # If pattern contains wildcards, use fnmatch
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(host, pattern):
                return True
        else:
            # No wildcards: exact match or subdomain match
            if host == pattern or host.endswith(f".{pattern}"):
                return True

    return False


def _get_safe_redirect_url(request: Request, allowed_domains: list[str], default: str = "/") -> str:
    """Get the next redirect URL from session, validating it's safe."""
    next_url = request.session.pop(SESSION_AUTH_NEXT, None)
    if next_url and _is_safe_redirect_url(next_url, allowed_domains):
        return next_url
    return default


async def _exchange_and_fetch(
    provider_key: str,
    settings,
    code: str,
    redirect_uri: str,
    code_verifier: str | None = None,
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    tenant: str | None = None,
    provider_type: str | None = None,
) -> tuple[NormalizedUserData, dict, dict]:
    """Compatibility wrapper around the generic OAuth flow helper.

    Args:
        provider_key: The OAuth provider identifier.
        settings: App settings (used to look up provider config if client_id/client_secret not given).
        code: The authorization code from the callback.
        redirect_uri: The redirect URI used in the auth request.
        code_verifier: PKCE code verifier (for Twitter).
        client_id: Override client_id (used during setup when settings aren't available).
        client_secret: Override client_secret (used during setup).
        tenant: Override tenant ID (used during setup for Microsoft).
        provider_type: Explicit provider type (used during setup when settings aren't loaded).

    Returns:
        Tuple of (NormalizedUserData, raw_user_info_dict, tokens_dict).
    """
    identity, user_info, tokens = await exchange_and_fetch_oauth_identity(
        provider_key,
        settings,
        code,
        redirect_uri,
        code_verifier,
        client_id=client_id,
        client_secret=client_secret,
        tenant=tenant,
        provider_type=provider_type,
    )
    return (
        NormalizedUserData(
            oauth_id=identity.subject_id,
            email=identity.email,
            name=identity.name,
            picture_url=identity.picture_url,
        ),
        user_info,
        tokens,
    )


def _set_login_session(request: Request, user: "User") -> None:
    """Rotate the session and populate it with user data.

    Preserves flash/flash_messages across the rotation so login
    success messages aren't lost.
    """
    finalize_authenticated_session(request, user)


class AuthController(Controller):
    path = "/auth"

    @get("/{provider:str}/login")
    async def oauth_login(
        self,
        request: Request,
        provider: str,
        next_url: Annotated[str | None, Parameter(query="next")] = None,
    ) -> Redirect | TemplateResponse:
        """Redirect to OAuth provider consent screen, or show dummy login form."""
        settings = get_settings()
        if provider not in settings.auth.get_method_keys():
            raise NotFoundException(f"Provider {provider} not configured")

        method = get_primary_auth_method(provider)
        return await method.begin_auth(request, next_url=next_url)

    @get("/{provider:str}/callback")
    async def oauth_callback(
        self,
        request: Request,
        db_session: AsyncSession,
        provider: str,
        code: str | None = None,
        oauth_state: Annotated[str | None, Parameter(query="state")] = None,
        error: str | None = None,
    ) -> Redirect:
        """Handle OAuth callback from provider."""
        settings = get_settings()
        if provider not in settings.auth.get_method_keys():
            raise NotFoundException(f"Provider {provider} not configured")

        if error:
            # Do not reflect the provider's `error` parameter into the UI —
            # it's attacker-influenceable and offers nothing the user can
            # act on. Log the raw reason for operator diagnostics.
            logger.info(
                "OAuth callback surfaced provider error for '%s': %s",
                provider,
                error,
            )
            flash_error(
                request,
                "Authentication with that provider failed. Please try again.",
            )
            return Redirect(path="/auth/login")

        method = get_primary_auth_method(provider)
        try:
            completion = await method.complete_auth(
                request,
                code=code,
                oauth_state=oauth_state,
                error=error,
            )
        except HTTPException:
            logger.warning(
                "Auth completion failed for provider '%s'; session_keys=%s",
                provider,
                list(request.session.keys()),
            )
            raise

        resolution = await find_or_create_user_for_identity(
            db_session,
            completion.identity,
            tokens=completion.tokens,
        )

        if isinstance(resolution, EmailVerificationRequired):
            # Email match without provider attestation — defer the actual
            # OAuth account link until the user proves control of the inbox
            # by clicking a short-lived signed URL.
            await db_session.commit()
            return await self._begin_email_link_challenge(request, settings, resolution)

        await db_session.commit()

        flash_success(request, "Successfully logged in!")
        return await _finalize_primary_login(
            request,
            db_session,
            settings,
            resolution,
            identity=completion.identity,
        )

    async def _begin_email_link_challenge(
        self,
        request: Request,
        settings,
        resolution: EmailVerificationRequired,
    ) -> Redirect:
        """Start the deferred-link email challenge and redirect to the pending page."""
        from skrift.auth.session_service import (
            PENDING_AUTH_STAGE_EMAIL_LINK_REQUIRED,
            begin_pending_authentication,
        )

        pending_auth = begin_pending_authentication(
            request,
            method_key=resolution.identity.method_key,
            method_type=resolution.identity.method_type,
            identity=resolution.identity,
            user_id=resolution.existing_user_id,
            is_new_user=False,
            stage=PENDING_AUTH_STAGE_EMAIL_LINK_REQUIRED,
        )

        email_backend = request.app.state.email_backend
        try:
            await begin_email_link_challenge(
                request,
                settings=settings,
                email_backend=email_backend,
                resolution=resolution,
                pending_auth_id=pending_auth.pending_auth_id,
                template_engine=request.app.template_engine.engine,
            )
        except Exception:
            logger.exception("Failed to dispatch email link challenge")
            clear_pending_authentication(request)
            clear_pending_link_state(request)
            flash_error(
                request,
                "We couldn't send the confirmation email. Please try again.",
            )
            return Redirect(path="/auth/login")

        return Redirect(path="/auth/verify-email/pending")

    @get("/login")
    async def login_page(
        self,
        request: Request,
        next_url: Annotated[str | None, Parameter(query="next")] = None,
    ) -> TemplateResponse:
        """Show login page with available providers."""
        flash = request.session.pop("flash", None)
        flash_messages = get_flash_messages(request)
        settings = get_settings()

        # Store next URL in session if provided and valid
        if next_url and _is_safe_redirect_url(next_url, settings.auth.allowed_redirect_domains):
            request.session[SESSION_AUTH_NEXT] = next_url

        # Get configured providers (excluding dummy from main list)
        configured_methods = settings.auth.get_method_keys()
        providers = {}
        for key in configured_methods:
            descriptor = get_primary_auth_method(key).get_descriptor()
            if descriptor.method_type == "dummy":
                continue
            providers[key] = descriptor

        # Check if dummy provider is configured
        has_dummy = any(
            settings.auth.get_primary_auth_method_type(key) == "dummy"
            for key in configured_methods
        )

        template_name = resolve_template_name(
            request.app.template_engine, "login.html", "auth/login.html"
        )
        return TemplateResponse(
            template_name,
            context={
                "flash": flash,
                "flash_messages": flash_messages,
                "providers": providers,
                "has_dummy": has_dummy,
            },
        )

    @post("/{provider:str}/options")
    async def begin_primary_method_options(
        self,
        request: Request,
        provider: str,
    ) -> Response:
        """Return browser auth options for interactive primary methods."""
        settings = get_settings()
        if provider not in settings.auth.get_method_keys():
            raise NotFoundException(f"Provider {provider} not configured")
        if settings.auth.get_primary_auth_method_type(provider) != "passkey":
            return _csrf_error(request, "unsupported_method")
        if not await verify_csrf(request):
            return _csrf_error(request, "invalid_csrf")

        try:
            options = begin_primary_passkey_authentication(request, settings, provider)
        except PasskeyRuntimeUnavailableError as exc:
            return _csrf_error(request, str(exc), status_code=503)

        return Response(
            content={
                "options": options,
                "csrf_token": request.session.get(CSRF_SESSION_KEY, ""),
            }
        )

    @post("/{provider:str}/register/options")
    async def begin_primary_method_registration(
        self,
        request: Request,
        db_session: AsyncSession,
        provider: str,
    ) -> Response:
        """Return browser registration options for primary passkey signup."""
        settings = get_settings()
        if provider not in settings.auth.get_method_keys():
            raise NotFoundException(f"Provider {provider} not configured")
        if settings.auth.get_primary_auth_method_type(provider) != "passkey":
            return _csrf_error(request, "unsupported_method")
        if not await verify_csrf(request):
            return _csrf_error(request, "invalid_csrf")

        form_data = await request.form()
        email = str(form_data.get("email", "")).strip().lower()
        name = str(form_data.get("name", "")).strip() or None
        if not email:
            return _csrf_error(request, "email_required")

        # Check for duplicate email BEFORE returning registration options — otherwise
        # the browser prompts for passkey creation and the authenticator saves the
        # credential even though the signup will fail in /register/complete, leaving
        # an orphan entry in the user's password manager. The response is a
        # generic `invalid_request` so an attacker cannot probe whether a given
        # email is already registered; the real reason is logged for diagnostics.
        existing = await db_session.execute(select(User).where(User.email == email))
        if existing.scalar_one_or_none() is not None:
            logger.info(
                "Passkey signup rejected: email already registered (email_hash=%s)",
                hashlib.sha256(email.encode()).hexdigest()[:16],
            )
            return _csrf_error(request, "invalid_request")

        try:
            options = begin_primary_passkey_registration(
                request,
                settings,
                method_key=provider,
                email=email,
                name=name,
            )
        except PasskeyRuntimeUnavailableError as exc:
            return _csrf_error(request, str(exc), status_code=503)

        return Response(
            content={
                "options": options,
                "csrf_token": request.session.get(CSRF_SESSION_KEY, ""),
            }
        )

    @post("/{provider:str}/register/complete")
    async def complete_primary_method_registration(
        self,
        request: Request,
        db_session: AsyncSession,
        provider: str,
    ) -> Response:
        """Create a new account from a primary passkey registration."""
        settings = get_settings()
        if provider not in settings.auth.get_method_keys():
            raise NotFoundException(f"Provider {provider} not configured")
        if settings.auth.get_primary_auth_method_type(provider) != "passkey":
            return _csrf_error(request, "unsupported_method")
        if not await verify_csrf(request):
            return _csrf_error(request, "invalid_csrf")

        signup_state = get_primary_passkey_registration_state(request)
        if signup_state is None or signup_state.method_key != provider:
            return _csrf_error(request, "registration_state_missing")

        form_data = await request.form()
        credential_raw = str(form_data.get("credential", "")).strip()
        if not credential_raw:
            return _csrf_error(request, "credential_required")

        try:
            credential = json.loads(credential_raw)
        except json.JSONDecodeError:
            return _csrf_error(request, "invalid_credential")

        try:
            registration = complete_primary_passkey_registration(
                request,
                settings,
                method_key=provider,
                credential=credential,
            )
            factor_key = _get_passkey_factor_key_for_method(settings, provider)
            login_result = await _create_primary_passkey_signup_login(
                db_session,
                method_key=provider,
                factor_key=factor_key,
                email=signup_state.email,
                name=signup_state.name,
                registration_result=registration,
            )
        except PasskeyRuntimeUnavailableError as exc:
            return _csrf_error(request, str(exc), status_code=503)
        except (PasskeyStateError, PasskeyVerificationError, ValueError) as exc:
            return _csrf_error(request, str(exc))

        redirect = await _finalize_primary_login(
            request,
            db_session,
            settings,
            login_result,
            identity=ResolvedPrimaryIdentity(
                method_key=provider,
                method_type="passkey",
                subject_id=registration.credential_id,
                email=login_result.user.email,
                name=login_result.user.name,
                picture_url=login_result.user.picture_url,
                raw_metadata={
                    "credential_id": registration.credential_id,
                    "factor_key": factor_key,
                },
                provided_fields={
                    field_name
                    for field_name, value in (
                        ("email", login_result.user.email),
                        ("name", login_result.user.name),
                        ("picture_url", login_result.user.picture_url),
                    )
                    if value
                },
            ),
        )
        await db_session.commit()
        return Response(content={"ok": True, "redirect": redirect.url}, status_code=201)

    @post("/{provider:str}/complete")
    async def complete_primary_method_auth(
        self,
        request: Request,
        db_session: AsyncSession,
        provider: str,
    ) -> Response:
        """Complete an interactive primary-auth method and start the login session."""
        settings = get_settings()
        if provider not in settings.auth.get_method_keys():
            raise NotFoundException(f"Provider {provider} not configured")
        if settings.auth.get_primary_auth_method_type(provider) != "passkey":
            return _csrf_error(request, "unsupported_method")
        if not await verify_csrf(request):
            return _csrf_error(request, "invalid_csrf")

        form_data = await request.form()
        credential_raw = str(form_data.get("credential", "")).strip()
        if not credential_raw:
            return _csrf_error(request, "credential_required")

        try:
            credential = json.loads(credential_raw)
        except json.JSONDecodeError:
            return _csrf_error(request, "invalid_credential")

        credential_id = str(credential.get("id") or credential.get("rawId") or "").strip()
        if not credential_id:
            return _csrf_error(request, "credential_id_required")

        factor_key = _get_passkey_factor_key_for_method(settings, provider)
        enrollment = await get_second_factor_enrollment_by_credential_id(
            db_session,
            factor_key=factor_key,
            credential_id=credential_id,
        )
        if enrollment is None:
            logger.info(
                "Primary passkey login: credential not enrolled (credential_id=%s)",
                credential_id[:16],
            )
            return _csrf_error(request, "invalid_credential")

        try:
            verification = complete_primary_passkey_authentication(
                request,
                settings,
                method_key=provider,
                enrollment=enrollment,
                credential=credential,
            )
        except PasskeyRuntimeUnavailableError as exc:
            return _csrf_error(request, str(exc), status_code=503)
        except (PasskeyStateError, PasskeyVerificationError) as exc:
            return _csrf_error(request, str(exc))

        login_result = await find_login_result_for_passkey_credential(
            db_session,
            factor_key=factor_key,
            method_key=provider,
            credential_id=credential_id,
        )
        if login_result is None:
            logger.info(
                "Primary passkey login: enrollment lacks bound user (credential_id=%s)",
                credential_id[:16],
            )
            return _csrf_error(request, "invalid_credential")

        touch_second_factor_enrollment(
            enrollment,
            sign_count=verification.new_sign_count,
            verification_metadata=verification.verification_metadata,
        )
        redirect = await _finalize_primary_login(
            request,
            db_session,
            settings,
            login_result,
            identity=ResolvedPrimaryIdentity(
                method_key=provider,
                method_type="passkey",
                subject_id=credential_id,
                email=login_result.user.email,
                name=login_result.user.name,
                picture_url=login_result.user.picture_url,
                raw_metadata={
                    "credential_id": credential_id,
                    "factor_key": factor_key,
                },
                provided_fields={
                    field_name
                    for field_name, value in (
                        ("email", login_result.user.email),
                        ("name", login_result.user.name),
                        ("picture_url", login_result.user.picture_url),
                    )
                    if value
                },
                can_create_account=False,
            ),
        )
        await db_session.commit()
        return Response(content={"ok": True, "redirect": redirect.url})

    @get("/passkeys")
    async def passkeys_page(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Redirect | TemplateResponse:
        """Render the passkey enrollment page for the current user."""
        user = await _get_authenticated_user(request, db_session)
        if user is None:
            flash_error(request, "Please log in to manage passkeys.")
            return Redirect(path="/auth/login?next=/auth/passkeys")

        settings = get_settings()
        factor_key = _get_default_passkey_factor_key(settings)
        if not factor_key:
            raise NotFoundException("No passkey second-factor method is configured")

        enrollments = await list_second_factor_enrollments_for_factor(
            db_session,
            str(user.id),
            factor_key,
        )
        method_descriptor = get_second_factor_method(factor_key).get_descriptor(settings)
        template_name = resolve_template_name(
            request.app.template_engine, "passkeys.html", "auth/passkeys.html"
        )
        return TemplateResponse(
            template_name,
            context={
                "user": user,
                "factor_key": factor_key,
                "descriptor": method_descriptor,
                "enrollments": enrollments,
                "flash_messages": get_flash_messages(request),
            },
        )

    @post("/passkeys/options")
    async def begin_passkey_registration(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Response:
        """Return browser registration options for a new passkey enrollment."""
        user = await _get_authenticated_user(request, db_session)
        if user is None:
            return Response(content={"error": "unauthorized"}, status_code=401)
        if not await verify_csrf(request):
            return Response(content={"error": "invalid_csrf"}, status_code=400)

        settings = get_settings()
        factor_key = _get_default_passkey_factor_key(settings)
        if not factor_key:
            return Response(content={"error": "passkey_not_configured"}, status_code=404)

        enrollments = await list_second_factor_enrollments_for_factor(
            db_session,
            str(user.id),
            factor_key,
        )
        try:
            options = begin_passkey_registration_flow(request, settings, user, enrollments)
        except PasskeyRuntimeUnavailableError as exc:
            return Response(content={"error": str(exc)}, status_code=503)

        return Response(
            content={
                "options": options,
                "csrf_token": request.session.get(CSRF_SESSION_KEY, ""),
            }
        )

    @post("/passkeys/complete")
    async def complete_passkey_registration(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Response:
        """Persist a verified passkey enrollment for the current user."""
        user = await _get_authenticated_user(request, db_session)
        if user is None:
            return Response(content={"error": "unauthorized"}, status_code=401)
        if not await verify_csrf(request):
            return Response(content={"error": "invalid_csrf"}, status_code=400)

        settings = get_settings()
        factor_key = _get_default_passkey_factor_key(settings)
        if not factor_key:
            return Response(content={"error": "passkey_not_configured"}, status_code=404)

        form_data = await request.form()
        display_name = str(form_data.get("display_name", "")).strip() or None
        credential_raw = str(form_data.get("credential", "")).strip()
        if not credential_raw:
            return Response(content={"error": "credential_required"}, status_code=400)

        try:
            credential = json.loads(credential_raw)
        except json.JSONDecodeError:
            return Response(content={"error": "invalid_credential"}, status_code=400)

        try:
            verification = complete_passkey_registration_flow(request, settings, user, credential)
            enrollment = await save_passkey_enrollment(
                db_session,
                user_id=str(user.id),
                factor_key=factor_key,
                display_name=display_name,
                credential_id=verification.credential_id,
                public_key=verification.public_key,
                sign_count=verification.sign_count,
                transports=verification.transports,
                enrollment_metadata=verification.enrollment_metadata,
            )
        except PasskeyRuntimeUnavailableError as exc:
            return Response(content={"error": str(exc)}, status_code=503)
        except (PasskeyStateError, PasskeyVerificationError, ValueError) as exc:
            return Response(content={"error": str(exc)}, status_code=400)

        await db_session.commit()
        return Response(
            content={
                "ok": True,
                "enrollment": {
                    "id": str(enrollment.id),
                    "display_name": enrollment.display_name or "",
                    "credential_id": enrollment.credential_id,
                },
            },
            status_code=201,
        )

    @get("/verify")
    async def verify_page(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Redirect | TemplateResponse:
        """Show available second-factor verification options for a pending auth session."""
        pending_auth = get_pending_authentication(request)
        if pending_auth is None or pending_auth.user_id is None:
            flash_error(request, "Your verification session is no longer available. Please log in again.")
            clear_pending_authentication(request)
            return Redirect(path="/auth/login")

        settings = get_settings()
        methods = await list_available_second_factor_descriptors(
            db_session,
            settings,
            pending_auth.user_id,
        )
        if not methods:
            flash_error(request, "No verification methods are available for this session. Please log in again.")
            clear_pending_authentication(request)
            return Redirect(path="/auth/login")

        template_name = resolve_template_name(
            request.app.template_engine, "verify.html", "auth/verify.html"
        )
        return TemplateResponse(
            template_name,
            context={
                "methods": methods,
                "pending_auth": pending_auth,
                "flash_messages": get_flash_messages(request),
            },
        )

    @get("/verify/{factor_key:str}")
    async def verify_method_page(
        self,
        request: Request,
        db_session: AsyncSession,
        factor_key: str,
    ) -> Redirect | TemplateResponse:
        """Render a factor-specific verification page for the pending auth session."""
        pending_auth = get_pending_authentication(request)
        if pending_auth is None or pending_auth.user_id is None:
            flash_error(request, "Your verification session is no longer available. Please log in again.")
            clear_pending_authentication(request)
            return Redirect(path="/auth/login")

        settings = get_settings()
        methods = await list_available_second_factor_descriptors(
            db_session,
            settings,
            pending_auth.user_id,
        )
        descriptor = next((method for method in methods if method.key == factor_key), None)
        if descriptor is None:
            flash_error(request, "That verification method is not available for this session.")
            return Redirect(path="/auth/verify")

        if not _is_passkey_factor(settings, factor_key):
            flash_error(request, "Second-factor verification is not implemented yet for this method.")
            return Redirect(path="/auth/verify")

        template_name = resolve_template_name(
            request.app.template_engine,
            "verify_passkey.html",
            "auth/verify_passkey.html",
        )
        return TemplateResponse(
            template_name,
            context={
                "descriptor": descriptor,
                "factor_key": factor_key,
                "pending_auth": pending_auth,
                "flash_messages": get_flash_messages(request),
            },
        )

    @post("/verify/{factor_key:str}/options")
    async def begin_second_factor_verification(
        self,
        request: Request,
        db_session: AsyncSession,
        factor_key: str,
    ) -> Response:
        """Return browser assertion options for a pending second-factor verification."""
        pending_auth = get_pending_authentication(request)
        if pending_auth is None or pending_auth.user_id is None:
            return Response(content={"error": "pending_auth_missing"}, status_code=401)
        if not await verify_csrf(request):
            return Response(content={"error": "invalid_csrf"}, status_code=400)

        settings = get_settings()
        if not _is_passkey_factor(settings, factor_key):
            return Response(content={"error": "unsupported_factor"}, status_code=400)

        result = await db_session.execute(
            select(User).where(User.id == UUID(pending_auth.user_id))
        )
        user = result.scalar_one_or_none()
        if user is None:
            logger.info(
                "Second-factor options: pending-auth user not found (user_id=%s)",
                pending_auth.user_id,
            )
            clear_pending_authentication(request)
            return Response(content={"error": "invalid_request"}, status_code=400)

        enrollments = await list_second_factor_enrollments_for_factor(
            db_session,
            pending_auth.user_id,
            factor_key,
        )
        if not enrollments:
            logger.info(
                "Second-factor options: user has no enrollments for factor (user_id=%s, factor=%s)",
                pending_auth.user_id,
                factor_key,
            )
            return Response(content={"error": "invalid_request"}, status_code=400)

        try:
            options = begin_passkey_authentication_flow(
                request,
                settings,
                user,
                pending_auth,
                enrollments,
            )
        except PasskeyRuntimeUnavailableError as exc:
            return Response(content={"error": str(exc)}, status_code=503)

        return Response(
            content={
                "options": options,
                "csrf_token": request.session.get(CSRF_SESSION_KEY, ""),
            }
        )

    @post("/verify/{factor_key:str}/complete")
    async def complete_second_factor_verification(
        self,
        request: Request,
        db_session: AsyncSession,
        factor_key: str,
    ) -> Response:
        """Verify a second-factor assertion and complete pending authentication."""
        pending_auth = get_pending_authentication(request)
        if pending_auth is None or pending_auth.user_id is None:
            return Response(content={"error": "pending_auth_missing"}, status_code=401)
        if not await verify_csrf(request):
            return Response(content={"error": "invalid_csrf"}, status_code=400)

        settings = get_settings()
        if not _is_passkey_factor(settings, factor_key):
            return Response(content={"error": "unsupported_factor"}, status_code=400)

        result = await db_session.execute(
            select(User).where(User.id == UUID(pending_auth.user_id))
        )
        user = result.scalar_one_or_none()
        if user is None:
            logger.info(
                "Second-factor complete: pending-auth user not found (user_id=%s)",
                pending_auth.user_id,
            )
            clear_pending_authentication(request)
            return Response(content={"error": "invalid_request"}, status_code=400)

        form_data = await request.form()
        credential_raw = str(form_data.get("credential", "")).strip()
        if not credential_raw:
            return Response(content={"error": "credential_required"}, status_code=400)

        try:
            credential = json.loads(credential_raw)
        except json.JSONDecodeError:
            return Response(content={"error": "invalid_credential"}, status_code=400)

        credential_id = str(credential.get("id") or credential.get("rawId") or "").strip()
        if not credential_id:
            return Response(content={"error": "credential_id_required"}, status_code=400)

        enrollment = await get_second_factor_enrollment_by_credential_id(
            db_session,
            factor_key=factor_key,
            credential_id=credential_id,
        )
        if enrollment is None or str(enrollment.user_id) != pending_auth.user_id:
            logger.info(
                "Second-factor complete: credential not found or not owned (credential_id=%s, user_id=%s)",
                credential_id[:16],
                pending_auth.user_id,
            )
            return Response(content={"error": "invalid_credential"}, status_code=400)

        try:
            verification = complete_passkey_authentication_flow(
                request,
                settings,
                user,
                pending_auth,
                enrollment,
                credential,
            )
        except PasskeyRuntimeUnavailableError as exc:
            return Response(content={"error": str(exc)}, status_code=503)
        except (PasskeyStateError, PasskeyVerificationError) as exc:
            return Response(content={"error": str(exc)}, status_code=400)

        touch_second_factor_enrollment(
            enrollment,
            sign_count=verification.new_sign_count,
            verification_metadata=verification.verification_metadata,
        )
        await db_session.commit()
        redirect = await _complete_pending_login(request, settings, user, pending_auth)
        return Response(content={"ok": True, "redirect": redirect.url})

    @post("/dummy-login")
    async def dummy_login_submit(
        self,
        request: Request,
        db_session: AsyncSession,
    ) -> Redirect:
        """Process dummy login form submission."""
        settings = get_settings()

        if DUMMY_PROVIDER_KEY not in settings.auth.get_method_keys():
            raise NotFoundException("Dummy provider not configured")

        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path="/auth/dummy/login")

        form_data = await request.form()
        email = form_data.get("email", "").strip()
        name = form_data.get("name", "").strip()

        if not email:
            flash_error(request, "Email is required")
            return Redirect(path="/auth/dummy/login")

        if not name:
            name = email.split("@")[0]

        oauth_id = f"dummy_{hashlib.sha256(email.encode()).hexdigest()[:16]}"
        dummy_metadata = {"id": oauth_id, "email": email, "name": name}

        # Dummy is dev-only (blocked in production). Treat its emails as
        # verified so the account-linking email challenge doesn't fire during
        # local development where no mailer is configured.
        dummy_identity = ResolvedPrimaryIdentity(
            method_key=DUMMY_PROVIDER_KEY,
            method_type="dummy",
            subject_id=oauth_id,
            email=email,
            name=name,
            picture_url=None,
            raw_metadata=dummy_metadata,
            provided_fields={"email", "name"},
            email_verified=True,
        )
        login_result = await find_or_create_user_for_identity(
            db_session, dummy_identity
        )
        # Dummy path never returns EmailVerificationRequired because
        # ``email_verified=True`` on the identity.
        assert isinstance(login_result, LoginResult)
        await db_session.commit()

        flash_success(request, "Successfully logged in!")
        return await _finalize_primary_login(
            request,
            db_session,
            settings,
            login_result,
            identity=dummy_identity,
        )

    @get("/verify-email/pending")
    async def verify_email_pending(self, request: Request) -> TemplateResponse | Redirect:
        """Render the 'check your inbox' page after an OAuth email challenge starts."""
        masked = get_pending_link_masked_email(request)
        if masked is None:
            flash_error(request, "No pending sign-in. Please start again.")
            return Redirect(path="/auth/login")
        template_name = resolve_template_name(
            request.app.template_engine,
            "verify_email_pending.html",
            "auth/verify_email_pending.html",
        )
        return TemplateResponse(template_name, context={"masked_email": masked})

    @get("/verify-email/claim/{token:str}")
    async def verify_email_claim(
        self,
        request: Request,
        db_session: AsyncSession,
        token: str,
    ) -> Redirect | TemplateResponse:
        """Complete the deferred OAuth account link after email verification.

        Strict checks (short-circuit on any failure with the same template):
          1. Token signature + expiry + ``purpose`` field.
          2. Revocation (via ``oauth2_service.is_token_revoked``).
          3. Session's pending-auth ID matches the token's bound ID — this is
             the primary defense against cross-browser / cross-user link
             interception; only the browser that started the OAuth flow can
             complete the link.
          4. The target user exists.
        """
        from skrift.db.services import oauth2_service

        settings = get_settings()

        def _invalid() -> TemplateResponse:
            template_name = resolve_template_name(
                request.app.template_engine,
                "verify_email_invalid.html",
                "auth/verify_email_invalid.html",
            )
            return TemplateResponse(template_name, context={})

        payload = verify_link_token(token, settings.secret_key)
        if payload is None:
            return _invalid()

        jti = payload.get("jti")
        if jti and await oauth2_service.is_token_revoked(db_session, jti):
            return _invalid()

        pending_auth = get_pending_authentication(request)
        if pending_auth is None:
            return _invalid()
        if pending_auth.pending_auth_id != payload.get("pending_auth_id"):
            return _invalid()

        target_user_id = payload.get("user_id_to_link")
        if not target_user_id:
            return _invalid()

        metadata = pop_pending_link_metadata(request)
        tokens = pop_pending_link_tokens(request)

        identity = ResolvedPrimaryIdentity(
            method_key=str(payload.get("provider_key", "")),
            method_type=str(payload.get("method_type", "oauth")),
            subject_id=str(payload.get("subject_id", "")),
            email=payload.get("email") or None,
            name=payload.get("name") or None,
            picture_url=payload.get("picture_url") or None,
            raw_metadata=metadata,
            provided_fields={"email"} if payload.get("email") else set(),
            email_verified=True,
        )

        try:
            login_result = await complete_verified_email_link(
                db_session,
                existing_user_id=str(target_user_id),
                identity=identity,
                tokens=tokens,
            )
        except ValueError:
            return _invalid()

        if jti:
            await oauth2_service.revoke_token(
                db_session, jti, "email_verify", expiry_from_payload(payload)
            )

        await db_session.commit()

        clear_pending_link_state(request)
        clear_pending_authentication(request)
        finalize_authenticated_session(request, login_result.user)
        flash_success(request, "Your account is linked.")

        return await _build_post_login_redirect(request, settings, login_result)

    @get("/logout")
    async def logout_confirm(self, request: Request) -> TemplateResponse:
        """Render a confirm form that POSTs back to /auth/logout with a CSRF token.

        GET is safe: a drive-by ``<img src="/auth/logout">`` no longer ends the
        session. Logout only happens on the CSRF-protected POST below.
        """
        template_name = resolve_template_name(
            request.app.template_engine, "logout_confirm.html", "auth/logout_confirm.html"
        )
        return TemplateResponse(template_name, context={})

    @post("/logout")
    async def logout(self, request: Request) -> Redirect | TemplateResponse:
        """Clear session and redirect to home, or render logout template if available."""
        if not await verify_csrf(request):
            flash_error(request, "Invalid request. Please try again.")
            return Redirect(path="/auth/logout")

        await hooks.do_action("before_logout", request)
        request.session.clear()
        try:
            template_name = resolve_template_name(
                request.app.template_engine, "logout.html", "auth/logout.html"
            )
            return TemplateResponse(template_name, context={})
        except TemplatesNotFound:
            return Redirect(path="/")
