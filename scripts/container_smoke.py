# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise the actual non-root image, packaged UI and primary language runtimes."""

import asyncio
import secrets
import subprocess

import httpx

from firefly_forge.api import create_application


async def main() -> None:
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
        assert "Firefly Forge" in (await client.get("/")).text
    print("Non-root container smoke passed: Studio, authenticated API, Python, Node.js, PHP and Composer")


if __name__ == "__main__":
    asyncio.run(main())
