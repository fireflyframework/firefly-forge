# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import importlib

import pytest


def module():
    try:
        return importlib.import_module("firefly_forge.browser_runner")
    except ImportError as exc:
        raise AssertionError("Actual isolated Playwright runner is missing") from exc


@pytest.mark.parametrize("path", ["https://example.com", "//example.com", "/../.env", "/.git/config", "/\x00bad"])
def test_browser_rejects_external_and_private_navigation_paths(path):
    with pytest.raises(ValueError):
        module().validate_browser_request(path, 390, 844, "dark")


def test_browser_script_declares_real_isolation_audits_and_fresh_context():
    script = module().browser_script(
        {
            "origin": "http://127.0.0.1:18080",
            "start_argv": ["python3", "-m", "http.server", "18080"],
            "path": "/",
            "width": 390,
            "height": 844,
            "theme": "dark",
            "startup_timeout": 10,
            "max_screenshot_bytes": 1000000,
        }
    )
    assert "require('/opt/forge-browser/node_modules/playwright')" in script
    assert "browser.newContext" in script
    assert "context.route" in script and "routeWebSocket" in script
    assert "getBoundingClientRect" in script
    assert "horizontal_overflow" in script and "clipped_text" in script and "overlap" in script
    assert "page.screenshot" in script and "no-sandbox" in script


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_BROWSER_IMAGE"),
    reason="Native pinned Playwright runner not configured",
)
@pytest.mark.parametrize("width,height,theme", [(390, 844, "dark"), (1440, 900, "light")])
async def test_actual_isolated_playwright_captures_theme_viewport_and_detects_layout_failures(
    store, tmp_path, width, height, theme
):
    import os

    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import BrowserInput, SandboxInput

    await store.acquire_ownership()
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "index.html").write_text("""<!doctype html><html><head><style>
body{background:white;color:black;font-family:Arial}
@media(prefers-color-scheme:dark){body{background:#111;color:white}}
h1{width:100px;white-space:nowrap;overflow:hidden}.wide{width:1200px}
button{position:fixed;bottom:10px;left:30px;width:100px;height:40px}
.cover{position:fixed;bottom:0;left:0;height:70px;width:100%;background:#f00}
</style></head><body><h1>Inventory reservation dashboard</h1><div class="wide">Actual layout fixture</div>
<a href="/">Stock list</a><button>Reserve</button><div class="cover">Footer covering action</div>
<img src="https://example.com/blocked.png"></body></html>""")
    start = [
        "node",
        "-e",
        "const h=require('http'),f=require('fs');h.createServer((q,r)=>{"
        "r.setHeader('Content-Type','text/html');r.end(f.readFileSync('index.html'))}).listen(18080,'127.0.0.1')",
    ]
    config = SandboxInput(
        mode="docker",
        context="colima-forge-tests",
        image=os.environ["FORGE_TEST_BROWSER_IMAGE"],
        browser=BrowserInput(
            image=os.environ["FORGE_TEST_BROWSER_IMAGE"], start_argv=start, origin="http://127.0.0.1:18080"
        ),
    )
    repo = await store.add_repository(RepositoryInput(name="product", url="https://github.com/team/product.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Inspect", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    browser = BrowserRunner(SandboxExecutor(store, journal), tmp_path / "evidence")
    result = await browser.check(run["id"], workspace, width=width, height=height, theme=theme)
    assert result["http_status"] == 200 and result["viewport"] == {"width": width, "height": height}
    expected = {"clipped_text", "overlap"} | ({"horizontal_overflow"} if width < 1200 else set())
    assert expected <= {finding["kind"] for finding in result["findings"]}
    assert result["container_network"] == "none" and result["browser_context"] == "fresh"
    assert "https://example.com" in result["blocked_origins"]
    assert result["artifact"]["filename"].endswith(".png")
    screenshot = browser.directory / result["artifact"]["filename"]
    assert screenshot.stat().st_mode & 0o777 == 0o600
    assert screenshot.read_bytes().startswith(b"\x89PNG")
    assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"

    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    from firefly_forge.agentic_runtime import execute_agentic
    from firefly_forge.workspace_tools import ToolRuntimeContext

    async def browser_callback(path, requested_width, requested_height, requested_theme):
        return await browser.check(
            run["id"], workspace, path=path, width=requested_width, height=requested_height, theme=requested_theme
        )

    def model(messages, info):
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returned:
            return ModelResponse(
                parts=[ToolCallPart("browser_check", {"path": "/", "width": width, "height": height, "theme": theme})]
            )
        if not any(part.tool_name == "write_file" for part in returned):
            actual = next(part.content for part in returned if part.tool_name == "browser_check")
            assert actual["browser_context"] == "fresh" and actual["artifact"]["media_type"] == "image/png"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "write_file",
                        {"path": "review.md", "content": "Fix the observed clipped heading and covered action."},
                    )
                ]
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"summary": "Actual browser evidence recorded"})]
        )

    async def emit(kind, message):
        pass

    summary, usage = await execute_agentic(
        workspace,
        "Inspect the real project UI",
        "pyfly",
        "none",
        emit,
        model=FunctionModel(model),
        runtime_context=ToolRuntimeContext(
            workspace, workspace, frozenset({"browser_check", "write_file"}), browser_runner=browser_callback
        ),
    )
    assert summary == "Actual browser evidence recorded" and usage["files_written"] == ["review.md"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_BROWSER_IMAGE"),
    reason="Native pinned Playwright runner not configured",
)
async def test_actual_journey_runs_javascript_actions_and_returns_state_images(store, tmp_path):
    import os

    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.qa import BrowserJourney
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import BrowserInput, SandboxInput

    await store.acquire_ownership()
    workspace = tmp_path / "journey-source"
    workspace.mkdir()
    (workspace / "index.html").write_text("""<!doctype html><html><body>
<label>Quantity<input id="quantity" aria-label="Quantity"></label>
<select id="item" aria-label="Item"><option value="apple">Apple</option><option value="pear">Pear</option></select>
<button id="reserve" onclick="
document.querySelector('#status').textContent='Reserved '+document.querySelector('#quantity').value">Reserve</button>
<p id="status">Available</p><a href="/done">Done</a></body></html>""")
    start = [
        "node",
        "-e",
        "require('http').createServer((q,r)=>{r.setHeader('Content-Type','text/html');"
        "r.end(require('fs').readFileSync('index.html'))}).listen(18080,'127.0.0.1')",
    ]
    config = SandboxInput(
        mode="docker",
        context="colima-forge-tests",
        image=os.environ["FORGE_TEST_BROWSER_IMAGE"],
        browser=BrowserInput(
            image=os.environ["FORGE_TEST_BROWSER_IMAGE"], start_argv=start, origin="http://127.0.0.1:18080"
        ),
    )
    repo = await store.add_repository(RepositoryInput(name="journey", url="https://github.com/team/journey.git"))
    run = await store.create_run(RunInput(repository_id=repo["id"], brief="Inspect journey", sandbox=config))
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    browser = BrowserRunner(SandboxExecutor(store, journal), tmp_path / "evidence")
    plan = BrowserJourney.model_validate(
        {
            "journey_id": "reserve",
            "name": "Reserve stock",
            "width": 390,
            "height": 844,
            "theme": "dark",
            "zoom": 125,
            "expected_states": [{"id": "reserved", "expected": "Reserved 2"}, {"id": "done", "expected": "Done route"}],
            "actions": [
                {"kind": "fill", "state_id": "entered", "role": "textbox", "name": "Quantity", "value": "2"},
                {"kind": "select", "state_id": "selected", "selector": "#item", "value": "pear"},
                {"kind": "press", "state_id": "focused", "value": "Tab"},
                {"kind": "click", "state_id": "clicked", "role": "button", "name": "Reserve"},
                {
                    "kind": "assert",
                    "state_id": "reserved",
                    "selector": "#status",
                    "assertion": "text",
                    "value": "Reserved 2",
                    "screenshot": True,
                },
                {"kind": "scroll", "state_id": "scrolled", "y": 100},
                {"kind": "navigate", "state_id": "navigated", "value": "/done"},
                {"kind": "assert", "state_id": "done", "assertion": "url", "value": "/done", "screenshot": True},
            ],
        }
    )
    evidence = await browser.journey(run["id"], workspace, plan)
    assert evidence.report["journey_id"] == "reserve" and evidence.report["zoom"] == 125
    assert len(evidence.images) == 2 and all(image.data.startswith(b"\x89PNG") for image in evidence.images)
    assert all(state["success"] for state in evidence.report["states"])
    assert {image.metadata["state_id"] for image in evidence.images} == {"reserved", "done"}
    assert evidence.report["not_reviewed"] == []
    assert evidence.report["reference_status"] == "reference_not_reviewed"
    assert (await journal.sandbox_executions(run["id"]))[0]["status"] == "removed"


@pytest.mark.parametrize(
    "svg",
    [
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/private"/></svg>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com";</style></svg>',
        '<!DOCTYPE svg [<!ENTITY secret SYSTEM "file:///etc/passwd">]><svg>&secret;</svg>',
    ],
)
def test_reference_svg_rejects_active_content_external_assets_and_entities(svg):
    with pytest.raises(ValueError):
        module().validate_reference_svg(svg)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not __import__("os").environ.get("FORGE_TEST_BROWSER_IMAGE"), reason="Pinned browser not configured"
)
async def test_actual_reference_raster_uses_embedded_font_and_reuses_private_hash_checked_cache(store, tmp_path):
    import os
    import struct

    from firefly_forge.browser_runner import BrowserRunner
    from firefly_forge.journal import Journal
    from firefly_forge.models import RepositoryInput, RunInput
    from firefly_forge.sandbox import SandboxExecutor
    from firefly_forge.sandbox_models import BrowserInput, SandboxInput

    await store.acquire_ownership()
    image = os.environ["FORGE_TEST_BROWSER_IMAGE"]
    repo = await store.add_repository(RepositoryInput(name="reference", url="https://github.com/team/reference.git"))
    run = await store.create_run(
        RunInput(
            repository_id=repo["id"],
            brief="Reference",
            sandbox=SandboxInput(
                mode="docker",
                context="colima-forge-tests",
                image=image,
                browser=BrowserInput(
                    image=image,
                    start_argv=["node", "-e", "process.exit()"],
                    origin="http://127.0.0.1:18080",
                    max_calls=1,
                ),
            ),
        )
    )
    journal = Journal(store)
    await journal.ensure_plan(run["id"])
    await journal.intent(run["id"], "implement")
    browser = BrowserRunner(SandboxExecutor(store, journal), tmp_path / "evidence")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 390 844">'
        '<rect width="390" height="844" fill="white"/>'
        '<text x="20" y="80" font-family="Manrope" font-size="30">Reviewed reference</text></svg>'
    )
    first = await browser.rasterize_reference(run["id"], tmp_path, svg, 390, 844)
    assert first.startswith(b"\x89PNG") and struct.unpack(">II", first[16:24]) == (390, 844)
    assert first == await browser.rasterize_reference(run["id"], tmp_path, svg, 390, 844)
    rows = await journal.sandbox_executions(run["id"])
    assert len(rows) == 1 and rows[0]["status"] == "removed"
    png = next(browser.directory.glob("reference-*.png"))
    png.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="cache"):
        await browser.rasterize_reference(run["id"], tmp_path, svg, 390, 844)
