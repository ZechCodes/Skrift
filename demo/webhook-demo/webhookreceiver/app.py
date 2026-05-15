"""Second site for the webhook demo.

It records incoming webhook calls and exposes controls for simulated receiver
errors, permanent failures, and response delays.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Annotated, Any

from litestar import Controller, Litestar, Request, get, post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect, Response


_events: deque[dict[str, Any]] = deque(maxlen=200)
_controls = {
    "mode": "ok",
    "delay_seconds": 0.0,
    "fail_next": 0,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _html() -> str:
    rows = "\n".join(
        f"""
        <tr>
            <td>{event["received_at"]}</td>
            <td>{event["status"]}</td>
            <td>{event["delivery_id"]}</td>
            <td>{event["event_type"]}</td>
            <td><pre>{event["payload"]}</pre></td>
        </tr>
        """
        for event in list(_events)
    )
    return f"""
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Webhook Receiver Demo</title>
        <style>
            body {{
                font-family: system-ui, sans-serif;
                margin: 0;
                background: #f6f7f8;
                color: #182026;
            }}
            main {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px; }}
            header {{ display: flex; justify-content: space-between; gap: 16px; }}
            .panel {{
                background: #fff;
                border: 1px solid #d9dee3;
                border-radius: 8px;
                padding: 18px;
                margin: 18px 0;
            }}
            .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }}
            button, input {{
                border: 1px solid #b8c0c8;
                border-radius: 6px;
                padding: 10px 12px;
                background: #fff;
            }}
            button.primary {{ background: #1f6feb; color: #fff; border-color: #1f6feb; }}
            table {{ width: 100%; border-collapse: collapse; background: #fff; }}
            th, td {{ border-bottom: 1px solid #e4e8ec; padding: 10px; text-align: left; }}
            pre {{ max-width: 420px; white-space: pre-wrap; margin: 0; }}
            .status {{ display: flex; gap: 14px; flex-wrap: wrap; }}
            .status span {{ background: #edf2f7; border-radius: 999px; padding: 6px 10px; }}
        </style>
        <meta http-equiv="refresh" content="4">
    </head>
    <body>
        <main>
            <header>
                <div>
                    <h1>Webhook Receiver</h1>
                    <p>Incoming calls from the Skrift sender site appear below.</p>
                </div>
                <p><a href="__SENDER_URL__">Sender site</a></p>
            </header>

            <section class="panel">
                <div class="status">
                    <span>Mode: <strong>{_controls["mode"]}</strong></span>
                    <span>Delay: <strong>{_controls["delay_seconds"]}s</strong></span>
                    <span>Fail next: <strong>{_controls["fail_next"]}</strong></span>
                    <span>Logged: <strong>{len(_events)}</strong></span>
                </div>
            </section>

            <section class="panel">
                <h2>Controls</h2>
                <form method="post" action="/controls" class="controls">
                    <button name="mode" value="ok" class="primary">OK</button>
                    <button name="mode" value="error">Transient 500</button>
                    <button name="mode" value="permanent">Permanent 410</button>
                    <label>
                        Delay seconds
                        <input name="delay_seconds" type="number" step="0.5" min="0" max="30"
                               value="{_controls["delay_seconds"]}">
                    </label>
                    <label>
                        Fail next
                        <input name="fail_next" type="number" min="0" max="20"
                               value="{_controls["fail_next"]}">
                    </label>
                    <button type="submit">Apply</button>
                </form>
                <form method="post" action="/clear">
                    <button type="submit">Clear log</button>
                </form>
            </section>

            <section class="panel">
                <h2>Incoming Webhooks</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Received</th>
                            <th>Response</th>
                            <th>Delivery</th>
                            <th>Event</th>
                            <th>Payload</th>
                        </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>
            </section>
        </main>
    </body>
    </html>
    """


def _sender_url(request: Request) -> str:
    host_header = request.headers.get("host", "localhost")
    if host_header.startswith("["):
        host = host_header.split("]", 1)[0] + "]"
    else:
        host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return f"{request.url.scheme}://{host}:8084"


class ReceiverController(Controller):
    path = "/"

    @get("/")
    async def index(self, request: Request) -> Response:
        return Response(
            _html().replace("__SENDER_URL__", _sender_url(request)),
            media_type="text/html",
        )

    @post("/webhook")
    async def webhook(self, request: Request) -> Response:
        if _controls["delay_seconds"]:
            await asyncio.sleep(float(_controls["delay_seconds"]))

        payload = await request.json()
        mode = _controls["mode"]
        if _controls["fail_next"] > 0:
            _controls["fail_next"] -= 1
            mode = "error"

        if mode == "permanent":
            status_code = 410
        elif mode == "error":
            status_code = 500
        else:
            status_code = 204

        _events.appendleft(
            {
                "received_at": _utcnow(),
                "status": status_code,
                "delivery_id": request.headers.get("x-skrift-delivery-id", ""),
                "event_type": request.headers.get("x-skrift-event-type", ""),
                "payload": payload,
            }
        )
        if status_code == 204:
            return Response(content=None, status_code=204)
        return Response({"status": status_code}, status_code=status_code)

    @post("/controls")
    async def controls(
        self,
        data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
    ) -> Redirect:
        mode = str(data.get("mode") or _controls["mode"])
        if mode in {"ok", "error", "permanent"}:
            _controls["mode"] = mode
        _controls["delay_seconds"] = max(0.0, float(data.get("delay_seconds") or 0))
        _controls["fail_next"] = max(0, int(data.get("fail_next") or 0))
        return Redirect("/")

    @post("/clear")
    async def clear(self) -> Redirect:
        _events.clear()
        return Redirect("/")


app = Litestar(route_handlers=[ReceiverController])
