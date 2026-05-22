"""Client Skrift site for the API permission grant demo."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Mapping
from typing import Annotated
from urllib.parse import urlencode

import httpx
from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Redirect, Response
from litestar.response import Template as TemplateResponse

from apigrantsdemo.permissions import (
    PERM_ANONYMOUS,
    PERM_DISALLOWED,
    PERM_ELEVATED,
    PERM_KNOWN,
)
from apigrantsdemo.seed_values import SEEDED_SERVICE_KEY


PROVIDER_PUBLIC_URL = "http://localhost:8091"
PROVIDER_INTERNAL_URL = "http://provider:8080"
CLIENT_PUBLIC_URL = "http://localhost:8092"


PERMISSION_CASES = {
    "anonymous": {
        "permission": PERM_ANONYMOUS,
        "title": "Allow Anonymous Service",
        "summary": "Can be requested without a pre-existing service API key.",
        "api_path": "/api/demo/anonymous",
    },
    "known": {
        "permission": PERM_KNOWN,
        "title": "Require Known Service",
        "summary": "Requires an existing service API key with this permission.",
        "api_path": "/api/demo/known",
    },
    "elevated": {
        "permission": PERM_ELEVATED,
        "title": "Require Elevated Security",
        "summary": "Requires a known service API key and user authorization.",
        "api_path": "/api/demo/elevated",
    },
    "disallowed": {
        "permission": PERM_DISALLOWED,
        "title": "Disallow API Grants",
        "summary": "Always rejected by the grant flow.",
        "api_path": "/api/demo/disallowed",
    },
}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _absolute_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _form_value(data: Mapping[str, object], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if isinstance(value, list | tuple):
        values = [str(item) for item in value if str(item)]
        return values[-1] if values else default
    return str(value)


async def _exchange_code(
    *,
    provider_internal_url: str,
    service_key: str,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict:
    headers = {}
    if service_key:
        headers["Authorization"] = f"Bearer {service_key}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            _absolute_url(provider_internal_url, "/api/grants/token"),
            headers=headers,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    payload["_status_code"] = response.status_code
    return payload


async def _call_provider_api(
    *,
    provider_internal_url: str,
    api_path: str,
    api_key: str,
) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            _absolute_url(provider_internal_url, api_path),
            headers={"Authorization": f"Bearer {api_key}"},
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    payload["_status_code"] = response.status_code
    return payload


class ClientDemoController(Controller):
    """Client site that requests API grants from the provider site."""

    path = "/"

    @get("/")
    async def index(self, request: Request) -> TemplateResponse:
        result = request.session.pop("grant_result", None)
        return TemplateResponse(
            "api-grants-demo/client.html",
            context={
                "request": request,
                "cases": PERMISSION_CASES,
                "provider_public_url": PROVIDER_PUBLIC_URL,
                "provider_internal_url": PROVIDER_INTERNAL_URL,
                "client_public_url": CLIENT_PUBLIC_URL,
                "seeded_service_key": SEEDED_SERVICE_KEY,
                "result": result,
            },
        )

    @get("/favicon.ico")
    async def favicon(self) -> Response[bytes]:
        return Response(content=b"", status_code=204)

    @post("/grant/start")
    async def start_grant(
        self,
        request: Request,
        data: Annotated[dict, Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        case_key = _form_value(data, "case")
        mode = _form_value(data, "mode", "without-key")
        case = PERMISSION_CASES.get(case_key)
        if case is None:
            request.session["grant_result"] = {"error": f"Unknown permission case: {case_key}"}
            return Redirect(path="/")

        provider_public_url = (_form_value(data, "provider_public_url") or PROVIDER_PUBLIC_URL).rstrip("/")
        provider_internal_url = (_form_value(data, "provider_internal_url") or PROVIDER_INTERNAL_URL).rstrip("/")
        client_public_url = (_form_value(data, "client_public_url") or CLIENT_PUBLIC_URL).rstrip("/")
        service_key = _form_value(data, "service_key").strip() if mode == "with-key" else ""
        service_name = (_form_value(data, "service_name") or "Skrift Grant Client Demo").strip()
        service_url = (_form_value(data, "service_url") or client_public_url).strip()
        redirect_uri = _absolute_url(client_public_url, "/grant/callback")
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)

        request.session["pending_api_grant"] = {
            "state": state,
            "verifier": verifier,
            "provider_internal_url": provider_internal_url,
            "service_key": service_key,
            "case": case_key,
            "redirect_uri": redirect_uri,
        }

        if mode == "with-key":
            headers = {"Authorization": f"Bearer {service_key}"} if service_key else {}
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    _absolute_url(provider_internal_url, "/api/grants/request"),
                    headers=headers,
                    json={
                        "redirect_uri": redirect_uri,
                        "permissions": [case["permission"]],
                        "service_name": service_name,
                        "service_url": service_url,
                        "state": state,
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                    },
                )
            if response.status_code != 200:
                request.session["grant_result"] = {
                    "case": case,
                    "mode": mode,
                    "error": response.text,
                    "status_code": response.status_code,
                }
                return Redirect(path="/")
            authorize_url = response.json()["authorize_url"]
            if authorize_url.startswith("/"):
                return Redirect(path=_absolute_url(provider_public_url, authorize_url))
            return Redirect(path=authorize_url)

        query = urlencode(
            {
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "permissions": case["permission"],
                "service_name": service_name,
                "service_url": service_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return Redirect(path=f"{provider_public_url}/api/grants/authorize?{query}")

    @get("/grant/callback")
    async def grant_callback(
        self,
        request: Request,
        code: str | None = None,
        grant_state: Annotated[str | None, Parameter(query="state")] = None,
        error: str | None = None,
    ) -> Redirect:
        pending = request.session.pop("pending_api_grant", None)
        if not pending:
            request.session["grant_result"] = {"error": "No pending grant session was found."}
            return Redirect(path="/")

        case = PERMISSION_CASES[pending["case"]]
        result = {"case": case, "mode": "with-key" if pending["service_key"] else "without-key"}

        if error:
            result["error"] = error
            request.session["grant_result"] = result
            return Redirect(path="/")
        if grant_state != pending["state"]:
            result["error"] = "State mismatch on grant callback."
            request.session["grant_result"] = result
            return Redirect(path="/")
        if not code:
            result["error"] = "Provider callback did not include a code."
            request.session["grant_result"] = result
            return Redirect(path="/")

        token_payload = await _exchange_code(
            provider_internal_url=pending["provider_internal_url"],
            service_key=pending["service_key"],
            code=code,
            redirect_uri=pending["redirect_uri"],
            verifier=pending["verifier"],
        )
        result["token_response"] = token_payload

        api_key = token_payload.get("key") if token_payload.get("_status_code") == 200 else ""
        if api_key:
            result["api_response"] = await _call_provider_api(
                provider_internal_url=pending["provider_internal_url"],
                api_path=case["api_path"],
                api_key=api_key,
            )

        request.session["grant_result"] = result
        return Redirect(path="/")

    @get("/{path:path}")
    async def fallback(self, path: str) -> Redirect:
        return Redirect(path="/")
