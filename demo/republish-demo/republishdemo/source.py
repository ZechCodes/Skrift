"""Source site for the republish demo."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Annotated
from urllib.parse import urlencode

import httpx
from litestar import Controller, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body, Parameter
from litestar.response import Redirect, Response
from litestar.response import Template as TemplateResponse

from skrift.republish import REPUBLISH_PERMISSION


SOURCE_PUBLIC_URL = "http://localhost:8093"
TARGET_PUBLIC_URL = "http://localhost:8094"
TARGET_INTERNAL_URL = "http://target:8080"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _absolute_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _form_value(data: dict, key: str, default: str = "") -> str:
    value = data.get(key, default)
    if isinstance(value, list | tuple):
        values = [str(item) for item in value if str(item)]
        return values[-1] if values else default
    return str(value)


def _demo_payload(source_public_url: str) -> dict:
    return {
        "canonical_url": _absolute_url(source_public_url, "/posts/republish-demo"),
        "title": "Republish Demo Post",
        "content": (
            "<p>This post was created by the source site and sent through "
            "Skrift's republish API.</p>"
        ),
        "summary": "A baseline repost payload sent from the source demo site.",
        "author_name": "Republish Demo",
        "updated_at": "2026-05-25T12:00:00Z",
        "image_url": _absolute_url(source_public_url, "/static/republish-demo-card.png"),
        "tags": ["demo", "republish"],
    }


async def _exchange_code(
    *,
    target_internal_url: str,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            _absolute_url(target_internal_url, "/api/grants/token"),
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


async def _call_republish_api(
    *,
    method: str,
    target_internal_url: str,
    api_key: str,
    payload: dict,
) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.request(
            method,
            _absolute_url(target_internal_url, "/api/republish/posts"),
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )

    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text}
    body["_status_code"] = response.status_code
    return body


class SourceDemoController(Controller):
    """Source site that obtains a republish grant and sends repost payloads."""

    path = "/"

    @get("/")
    async def index(self, request: Request) -> TemplateResponse:
        result = request.session.pop("republish_result", None)
        return TemplateResponse(
            "republish-demo/source.html",
            context={
                "request": request,
                "source_public_url": SOURCE_PUBLIC_URL,
                "target_public_url": TARGET_PUBLIC_URL,
                "target_internal_url": TARGET_INTERNAL_URL,
                "result": result,
                "api_key": request.session.get("republish_api_key", ""),
                "demo_payload": _demo_payload(SOURCE_PUBLIC_URL),
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
        target_public_url = (_form_value(data, "target_public_url") or TARGET_PUBLIC_URL).rstrip("/")
        source_public_url = (_form_value(data, "source_public_url") or SOURCE_PUBLIC_URL).rstrip("/")
        redirect_uri = _absolute_url(source_public_url, "/grant/callback")
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)

        request.session["pending_republish_grant"] = {
            "state": state,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "target_internal_url": (
                _form_value(data, "target_internal_url") or TARGET_INTERNAL_URL
            ).rstrip("/"),
        }

        query = urlencode(
            {
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "permissions": REPUBLISH_PERMISSION,
                "service_name": "Republish Demo Source",
                "service_url": source_public_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return Redirect(path=f"{target_public_url}/api/grants/authorize?{query}")

    @get("/grant/callback")
    async def grant_callback(
        self,
        request: Request,
        code: str | None = None,
        grant_state: Annotated[str | None, Parameter(query="state")] = None,
        error: str | None = None,
    ) -> Redirect:
        pending = request.session.pop("pending_republish_grant", None)
        result = {"step": "grant"}
        if not pending:
            result["error"] = "No pending republish grant session was found."
        elif error:
            result["error"] = error
        elif grant_state != pending["state"]:
            result["error"] = "State mismatch on grant callback."
        elif not code:
            result["error"] = "Target callback did not include a code."
        else:
            token_payload = await _exchange_code(
                target_internal_url=pending["target_internal_url"],
                code=code,
                redirect_uri=pending["redirect_uri"],
                verifier=pending["verifier"],
            )
            result["token_response"] = token_payload
            api_key = token_payload.get("key") if token_payload.get("_status_code") == 200 else ""
            if api_key:
                request.session["republish_api_key"] = api_key

        request.session["republish_result"] = result
        return Redirect(path="/")

    @post("/republish/upsert")
    async def upsert(self, request: Request) -> Redirect:
        api_key = request.session.get("republish_api_key", "")
        if not api_key:
            request.session["republish_result"] = {
                "step": "upsert",
                "error": "Authorize republishing before sending a post.",
            }
            return Redirect(path="/")

        payload = _demo_payload(SOURCE_PUBLIC_URL)
        request.session["republish_result"] = {
            "step": "upsert",
            "payload": payload,
            "api_response": await _call_republish_api(
                method="PUT",
                target_internal_url=TARGET_INTERNAL_URL,
                api_key=api_key,
                payload=payload,
            ),
        }
        return Redirect(path="/")

    @post("/republish/delete")
    async def delete(self, request: Request) -> Redirect:
        api_key = request.session.get("republish_api_key", "")
        if not api_key:
            request.session["republish_result"] = {
                "step": "delete",
                "error": "Authorize republishing before deleting a post.",
            }
            return Redirect(path="/")

        payload = {"canonical_url": _demo_payload(SOURCE_PUBLIC_URL)["canonical_url"]}
        request.session["republish_result"] = {
            "step": "delete",
            "payload": payload,
            "api_response": await _call_republish_api(
                method="DELETE",
                target_internal_url=TARGET_INTERNAL_URL,
                api_key=api_key,
                payload=payload,
            ),
        }
        return Redirect(path="/")

    @get("/{path:path}")
    async def fallback(self, path: str) -> Redirect:
        return Redirect(path="/")
