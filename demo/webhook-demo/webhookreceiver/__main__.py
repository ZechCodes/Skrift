"""Run the webhook receiver demo site."""

from __future__ import annotations

import asyncio

from hypercorn.asyncio import serve
from hypercorn.config import Config

from webhookreceiver.app import app


async def main() -> None:
    config = Config()
    config.bind = ["0.0.0.0:8090"]
    await serve(app, config)


if __name__ == "__main__":
    asyncio.run(main())
