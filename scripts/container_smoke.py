# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise the actual non-root image, packaged UI and primary language runtimes."""

import asyncio
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

from firefly_forge.api import create_application
from firefly_forge.product_fonts import manrope_license, manrope_style, manrope_wordmark


def check_process_supervision() -> None:
    """An exiting grandchild must be adopted and reaped by the image's PID1."""
    assert os.getuid() == 10001, "Container command must remain the forge user"
    assert sys.platform == "linux", "Run this probe in the actual Linux image"
    orphan = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,time; child=os.fork(); "
            "(time.sleep(0.1),os._exit(0)) if child==0 else "
            "(print(child,flush=True),os._exit(0))",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    pid = int(orphan.stdout.strip())
    deadline = time.monotonic() + 5
    state = Path(f"/proc/{pid}/stat")
    while state.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not state.exists(), f"Exited orphan {pid} was not reaped: {state.read_text() if state.exists() else ''}"
    assert Path("/proc/1/comm").read_text().strip() == "tini", "Image must provide its own PID1 init"
    print("Image supervision passed: non-root command and orphan PID removal")


async def main() -> None:
    check_process_supervision()
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
    if sys.argv[1:] == ["--supervision-only"]:
        check_process_supervision()
    else:
        assert not sys.argv[1:], "Unknown smoke arguments"
        asyncio.run(main())
