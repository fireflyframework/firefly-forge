# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise the actual non-root image, packaged UI and primary language runtimes."""

import asyncio
import os
import secrets
import subprocess

import httpx

from firefly_forge.api import create_application
from firefly_forge.product_fonts import manrope_license, manrope_style, manrope_wordmark


async def main() -> None:
    assert os.getuid() != 0
    assert '<path d="' in manrope_wordmark("forge", "#262821")
    assert "data:font/woff2;base64," in manrope_style()
    assert "SIL OPEN FONT LICENSE" in manrope_license()
    for command in [["node", "--version"], ["npm", "--version"], ["php", "--version"], ["composer", "--version"]]:
        subprocess.run(command, check=True, capture_output=True)
    token = secrets.token_urlsafe(32)
    app = create_application(token=token)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client,
    ):
        assert (await client.get("/api/v1/health")).status_code == 401
        assert (await client.get("/api/v1/health", headers={"Authorization": f"Bearer {token}"})).status_code == 200
        assert "firefly forge" in (await client.get("/")).text
        font = await client.get("/fonts/manrope-latin-wght-normal.woff2")
        assert font.status_code == 200 and font.content.startswith(b"wOF2")
    print("Non-root container smoke passed: Studio, Manrope brand generation, authenticated API and language runtimes")


if __name__ == "__main__":
    asyncio.run(main())
