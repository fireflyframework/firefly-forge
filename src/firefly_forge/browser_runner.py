# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""A fresh Playwright context inspecting only a container-local project origin."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import struct
import uuid
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

from pyfly.container import service

from firefly_forge.qa import BrowserJourney, EvidenceImage, JourneyEvidence
from firefly_forge.qa import validate_browser_request as validate_browser_request
from firefly_forge.sandbox import SandboxExecutor
from firefly_forge.sandbox_models import validate_sandbox_snapshot
from firefly_forge.secrets import contains_known_credential

_BROWSER_JS = r"""
const {chromium}=require('/opt/forge-browser/node_modules/playwright');
const {spawn}=require('node:child_process');
const config=CONFIG;
let server,browser;
async function main(){
 server=spawn(config.start_argv[0],config.start_argv.slice(1),{stdio:['ignore','pipe','pipe'],detached:true});
 server.on('error',()=>{});
 // Drain project logs without admitting them as control-plane evidence.
 server.stdout.on('data',()=>{});server.stderr.on('data',()=>{});
 const deadline=Date.now()+config.startup_timeout*1000;
 let ready=false;
 while(Date.now()<deadline){
  try{await fetch(config.origin,{redirect:'manual',signal:AbortSignal.timeout(1000)});ready=true;break;}catch{}
  if(server.exitCode!==null)throw new Error('Project server exited before browser inspection');
  await new Promise(r=>setTimeout(r,100));
 }
 if(!ready)throw new Error('Project server did not become reachable');
 browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:config.width,height:config.height},
  colorScheme:config.theme,reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false});
 const blocked=new Set();
 await context.route('**/*',route=>{
  const url=new URL(route.request().url());
  if(url.origin!==config.origin){blocked.add(url.origin);return route.abort();}
  return route.continue();
 });
 await context.routeWebSocket('**/*',ws=>{blocked.add('websocket');ws.close();});
 const page=await context.newPage();
 const response=await page.goto(config.origin+config.path,{waitUntil:'networkidle',timeout:20000});
 await page.evaluate(()=>document.fonts.ready);
 const findings=await page.evaluate(()=>{
  const results=[];
  const add=(kind,element,detail)=>{if(results.length<100)results.push({kind,
   selector:element.tagName.toLowerCase()+(element.id?'#'+element.id:''),detail});};
  if(document.documentElement.scrollWidth>innerWidth+1)add('horizontal_overflow',document.documentElement,
   {width:document.documentElement.scrollWidth,viewport:innerWidth});
  const visible=[...document.querySelectorAll('h1,h2,h3,button,a,label,input,select,[role="button"]')]
   .filter(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';}).slice(0,300);
  for(const el of visible){
   const r=el.getBoundingClientRect(),style=getComputedStyle(el);
   if((style.overflowX==='hidden'||style.overflowX==='clip')&&el.scrollWidth>el.clientWidth+1)
    add('clipped_text',el,{width:el.clientWidth,content:el.scrollWidth});
   const range=document.createRange();range.selectNodeContents(el);
   if(el.matches('h1,h2,h3,button,a,label')&&style.overflowY!=='visible'&&range.getBoundingClientRect().height>r.height+2)
    add('clipped_text',el,{height:r.height,content:range.getBoundingClientRect().height});
   if(el.matches('button,a,input,select,[role="button"]')&&r.top>=0&&r.bottom<=innerHeight){
    const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
    if(top&&top!==el&&!el.contains(top)&&!top.contains(el))add('overlap',el,{covered_by:top.tagName.toLowerCase()});
   }
  }
  return results;
 });
 await page.keyboard.press('Tab');
 const focus=await page.evaluate(()=>({tag:document.activeElement.tagName.toLowerCase(),
  visible:document.activeElement.getBoundingClientRect().width>0}));
 const screenshot=await page.screenshot({type:'png',fullPage:false,animations:'disabled'});
 if(screenshot.length>config.max_screenshot_bytes)throw new Error('Screenshot exceeds its frozen size limit');
 console.log(JSON.stringify({schema:1,origin:config.origin,path:config.path,viewport:{width:config.width,height:config.height},
  theme:config.theme,http_status:response?.status()||0,findings,focus,blocked_origins:[...blocked].slice(0,20),
  browser_context:'fresh',container_network:'none',chromium_sandbox:false,screenshot:screenshot.toString('base64')}));
}
main().catch(()=>{console.error('Isolated project browser inspection failed');process.exitCode=1;})
 .finally(async()=>{if(browser)await browser.close();
  if(server?.pid){try{process.kill(-server.pid,'SIGKILL');}catch{}}});
"""


_JOURNEY_JS = r"""
const {chromium}=require('/opt/forge-browser/node_modules/playwright');

const {spawn}=require('node:child_process');

const config=CONFIG;
let server,browser;

async function main(){
 server=spawn(config.start_argv[0],config.start_argv.slice(1),{stdio:['ignore','pipe','pipe'],detached:true});

 server.on('error',()=>{});
server.stdout.on('data',()=>{});
server.stderr.on('data',()=>{});

 const deadline=Date.now()+config.startup_timeout*1000;
let ready=false;

 while(Date.now()<deadline){try{await fetch(config.origin,{redirect:'manual',signal:AbortSignal.timeout(1000)});
ready=true;
break;
}catch{}
  if(server.exitCode!==null)throw Error('server exited');
await new Promise(r=>setTimeout(r,100));
}
 if(!ready)throw Error('server unavailable');

 browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});

 const context=await browser.newContext({viewport:{width:config.width,height:config.height},colorScheme:config.theme,
  reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false});

 const blocked=new Set();
await context.route('**/*',route=>{
  const url=new URL(route.request().url());
if(url.origin!==config.origin){blocked.add(url.origin);
return route.abort();
}
  return route.continue();
});
await context.routeWebSocket('**/*',ws=>{blocked.add('websocket');
ws.close();
});

 const page=await context.newPage();
page.on('dialog',d=>d.dismiss());

 await page.addInitScript(zoom=>{document.addEventListener('DOMContentLoaded',()=>{
 document.documentElement.style.zoom=String(zoom/100);
});
},config.zoom);

 await page.goto(config.origin+config.path,{waitUntil:'networkidle',timeout:20000});
await page.evaluate(()=>document.fonts.ready);

 const states=[],screenshots=[];
let bytes=0;

 for(const action of config.actions){
  page.setDefaultTimeout(action.timeout_ms);
let success=true,error=null;

  const target=action.role?
 page.getByRole(action.role,action.name?{name:action.name,exact:true}:{}):
 action.selector?page.locator(action.selector):null;

  try{
   if(action.kind==='navigate')await page.goto(config.origin+action.value,
 {waitUntil:'networkidle',timeout:action.timeout_ms});

   else if(action.kind==='click')await target.click();

   else if(action.kind==='fill')await target.fill(action.value);

   else if(action.kind==='select')await target.selectOption(action.value);

   else if(action.kind==='press')await (target||page.keyboard).press(action.value);

   else if(action.kind==='scroll')await page.mouse.wheel(action.x,action.y);

   else if(action.kind==='assert'){
    const a=action.assertion;

    if(a==='visible'||a==='hidden')await target.waitFor({state:a,timeout:action.timeout_ms});

    else if(a==='url'){if(page.url()!==config.origin+action.value)throw Error('URL assertion failed');
}
    else if(a==='count'){if(await target.count()!==Number(action.value))throw Error('Count assertion failed');
}
    else if(a==='text'){if((await target.innerText()).trim()!==action.value)throw Error('Text assertion failed');
}
    else if(a==='value'){if(await target.inputValue()!==action.value)throw Error('Value assertion failed');
}
    else if(a==='enabled'||a==='disabled'){
 if(await target.isEnabled()!==(a==='enabled'))throw Error('Enabled assertion failed');
}
    else if(a==='checked'||a==='unchecked'){
 if(await target.isChecked()!==(a==='checked'))throw Error('Checked assertion failed');
}
   }
  }catch{success=false;
error='Requested DOM action or assertion failed';
}
  const dom=await page.evaluate(()=>{
   const nodes=[],findings=[];
const visible=[...document.querySelectorAll(
 'h1,h2,h3,button,a,label,input,select,[role]')].slice(0,100);

   if(document.documentElement.scrollWidth>innerWidth+1)findings.push({kind:'horizontal_overflow'});

   for(const el of visible){const r=el.getBoundingClientRect(),s=getComputedStyle(el);
if(!r.width||!r.height||s.visibility==='hidden')continue;

    nodes.push({tag:el.tagName.toLowerCase(),id:el.id,role:el.getAttribute('role'),text:(el.textContent||'').trim().slice(0,300),
     value:el.matches('input,select')&&el.type!=='password'?String(el.value).slice(0,300):null,
     rect:{x:r.x,y:r.y,width:r.width,height:r.height},focused:el===document.activeElement});

    if(['hidden','clip'].includes(s.overflowX)&&el.scrollWidth>el.clientWidth+1)findings.push({kind:'clipped_text',id:el.id});

    if(el.matches('button,a,input,select,[role="button"]')&&r.top>=0&&r.bottom<=innerHeight){
     const top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);

     if(top&&top!==el&&!el.contains(top)&&!top.contains(el))findings.push({kind:'overlap',id:el.id});
}
   }
   return {nodes,findings:findings.slice(0,100),focus:document.activeElement.tagName.toLowerCase(),
    title:document.title.slice(0,300),body_text:(document.body?.innerText||'').slice(0,50000)};

  });

  const state={state_id:action.state_id,kind:action.kind,success,error,path:new URL(page.url()).pathname,dom};

  if(action.screenshot){const image=await page.screenshot({type:'png',fullPage:false,animations:'disabled'});

   bytes+=image.length;
if(image.length>config.max_screenshot_bytes||bytes>6000000)throw Error('image limit');

   screenshots.push({state_id:action.state_id,data:image.toString('base64')});
}
  states.push(state);
if(!success)break;

 }
 console.log(JSON.stringify({schema:2,journey_id:config.journey_id,origin:config.origin,path:config.path,
  viewport:{width:config.width,height:config.height},theme:config.theme,zoom:config.zoom,zoom_mode:'css_document',
  states,screenshots,blocked_origins:[...blocked].slice(0,20),browser_context:'fresh',container_network:'none',chromium_sandbox:false}));

}
main().catch(()=>{console.error('Isolated project journey failed');
process.exitCode=1;
}).finally(async()=>{
 if(browser)await browser.close();
if(server?.pid){try{process.kill(-server.pid,'SIGKILL');
}catch{}}});

"""

_RASTER_JS = r"""
const {chromium}=require('/opt/forge-browser/node_modules/playwright');
const config=JSON.parse(require('fs').readFileSync(0,'utf8'));let browser;
async function main(){
 browser=await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:config.width,height:config.height},
  colorScheme:config.theme,reducedMotion:'reduce',serviceWorkers:'block',acceptDownloads:false});
 await context.route('**/*',route=>route.abort());
 const page=await context.newPage();
 await page.setContent('<!doctype html><html><head><style>html,body{margin:0;padding:0;}'+config.font+
  '</style></head><body>'+config.svg+'</body></html>',{waitUntil:'load'});
 await page.evaluate(zoom=>{
  document.documentElement.style.zoom=String(zoom/100);
  const svg=document.querySelector('svg');svg.style.width='100%';svg.style.height='100vh';
 },config.zoom);
 await page.evaluate(()=>document.fonts.ready);
 const image=await page.screenshot({type:'png',fullPage:false,animations:'disabled'});
 if(image.length>config.max_bytes)throw Error('Reference screenshot limit');
 console.log(JSON.stringify({width:config.width,height:config.height,image:image.toString('base64')}));
}
main().catch(()=>{console.error('Isolated reference raster failed');process.exitCode=1;})
 .finally(async()=>{if(browser)await browser.close();});
"""


def validate_reference_svg(svg: str) -> None:
    from firefly_forge.product_fonts import manrope_style

    if len(svg.encode("utf-8")) > 2_000_000 or "<!DOCTYPE" in svg.upper() or "<!ENTITY" in svg.upper():
        raise ValueError("Reference SVG must be bounded and contain no entities")
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError("Reference SVG is malformed") from exc
    allowed = {
        "svg",
        "g",
        "rect",
        "path",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "title",
        "desc",
        "defs",
        "clipPath",
        "linearGradient",
        "radialGradient",
        "stop",
        "style",
    }
    nodes = list(root.iter())
    if root.tag != "{http://www.w3.org/2000/svg}svg" or len(nodes) > 5000:
        raise ValueError("Reference SVG requires a bounded SVG document")
    for element in nodes:
        tag = element.tag.removeprefix("{http://www.w3.org/2000/svg}")
        if tag not in allowed:
            raise ValueError("Reference SVG active content or external asset is forbidden")
        if tag == "style" and (element.text or "").strip() != manrope_style().strip():
            raise ValueError("Reference SVG permits only the bundled licensed font stylesheet")
        for key, value in element.attrib.items():
            local = key.split("}")[-1].lower()
            if local.startswith("on") or local in {"href", "src", "style"}:
                raise ValueError("Reference SVG event handlers and asset links are forbidden")
            fragment = value[5:-1] if value.startswith("url(#") and value.endswith(")") else ""
            safe_fragment = fragment and all(c.isascii() and (c.isalnum() or c in "_-") for c in fragment)
            if (
                ("url(" in value.lower() and not safe_fragment)
                or "javascript:" in value.lower()
                or "@import" in value.lower()
            ):
                raise ValueError("Reference SVG cannot load external styles or resources")


def _reference_png(data: bytes, width: int, height: int, limit: int) -> None:
    if (
        len(data) > limit
        or len(data) < 33
        or not data.startswith(b"\x89PNG\r\n\x1a\n")
        or data[12:16] != b"IHDR"
        or struct.unpack(">II", data[16:24]) != (width, height)
    ):
        raise ValueError("Reference PNG does not match its bounded viewport")


def _private_read(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError("Reference cache requires bounded private regular files")
        return source.read(maximum + 1)


def browser_script(configuration: dict[str, Any]) -> str:
    return _BROWSER_JS.replace("CONFIG", json.dumps(configuration, separators=(",", ":")))


@service
class BrowserRunner:
    def __init__(self, executor: SandboxExecutor, directory: Path) -> None:
        self.executor = executor
        self.directory = directory / "browser-evidence"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Browser evidence requires a private real directory")
        self.directory.chmod(0o700)
        self._reference_locks: dict[str, asyncio.Lock] = {}

    async def check(
        self,
        run_id: str,
        workspace_root: Path,
        *,
        task_id: str = "implement",
        path: str = "/",
        width: int = 1440,
        height: int = 900,
        theme: str = "light",
    ) -> dict[str, Any]:
        validate_browser_request(path, width, height, theme)
        run = await self.executor.store.get_run(run_id)
        config = validate_sandbox_snapshot(run["sandbox_snapshot"])
        if config.mode != "docker" or config.browser is None:
            raise ValueError("Browser checks are not configured for this run")
        if width > config.browser.max_width or height > config.browser.max_height:
            raise ValueError("Browser viewport exceeds the frozen run policy")
        payload = {
            **config.browser.model_dump(mode="json"),
            "path": path,
            "width": width,
            "height": height,
            "theme": theme,
        }
        script = browser_script(payload)
        result = await self.executor.execute(
            run_id,
            workspace_root,
            workspace_root,
            ["node", "-e", script],
            config.browser.startup_timeout + 60,
            task_id,
            _browser=True,
        )
        if result.exit_code:
            raise ValueError("Configured isolated browser runner failed; no successful visual evidence was admitted")
        try:
            record: dict[str, Any] = json.loads(result.output)
            screenshot = base64.b64decode(record.pop("screenshot"), validate=True)
            if not screenshot.startswith(b"\x89PNG\r\n\x1a\n") or len(screenshot) > config.browser.max_screenshot_bytes:
                raise ValueError("Browser screenshot is invalid or oversized")
            if (
                record["origin"] != config.browser.origin
                or record["viewport"] != {"width": width, "height": height}
                or record["theme"] != theme
            ):
                raise ValueError("Browser evidence does not match its requested origin or viewport")
            if not isinstance(record["findings"], list) or len(record["findings"]) > 100:
                raise ValueError("Browser findings exceed the evidence bounds")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("Browser runner returned invalid bounded evidence") from exc
        evidence_id = uuid.uuid4().hex
        image_path = self.directory / (evidence_id + ".png")
        fd = os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as image:
            image.write(screenshot)
        record.update(
            evidence_id=evidence_id,
            screenshot_sha256=hashlib.sha256(screenshot).hexdigest(),
            artifact={"id": evidence_id, "filename": image_path.name, "media_type": "image/png"},
        )
        return record

    async def journey(
        self, run_id: str, workspace_root: Path, plan: BrowserJourney, *, task_id: str = "implement"
    ) -> JourneyEvidence:
        plan = BrowserJourney.model_validate(plan)
        run = await self.executor.store.get_run(run_id)
        config = validate_sandbox_snapshot(run["sandbox_snapshot"])
        if config.mode != "docker" or config.browser is None:
            raise ValueError("Browser journeys require the frozen isolated browser configuration")
        if plan.width > config.browser.max_width or plan.height > config.browser.max_height:
            raise ValueError("Journey viewport exceeds the frozen run policy")
        if contains_known_credential(plan.model_dump(mode="json"), self.executor.credential_registry()):
            raise ValueError("Journey actions cannot contain configured credentials")
        payload = {**config.browser.model_dump(mode="json"), **plan.model_dump(mode="json")}
        encoded = base64.b64encode(zlib.compress(json.dumps(payload, separators=(",", ":")).encode())).decode()
        expression = "JSON.parse(require('zlib').inflateSync(Buffer.from('" + encoded + "','base64')).toString())"
        script = _JOURNEY_JS.replace("CONFIG", expression)
        if len(script) > 8192:
            raise ValueError("Journey plan exceeds the bounded runner argument size; split it into smaller calls")
        result = await self.executor.execute(
            run_id,
            workspace_root,
            workspace_root,
            ["node", "-e", script],
            config.browser.startup_timeout + 150,
            task_id,
            _browser=True,
        )
        if result.exit_code:
            raise ValueError("Configured isolated browser journey failed; no visual evidence admitted")
        try:
            record: dict[str, Any] = json.loads(result.output)
            if (
                record["schema"] != 2
                or record["journey_id"] != plan.journey_id
                or record["origin"] != config.browser.origin
                or record["viewport"] != {"width": plan.width, "height": plan.height}
                or record["theme"] != plan.theme
                or record["zoom"] != plan.zoom
            ):
                raise ValueError("Journey evidence metadata mismatch")
            states, encoded = record["states"], record.pop("screenshots")
            if not isinstance(states, list) or len(states) > len(plan.actions) or not isinstance(encoded, list):
                raise ValueError("Invalid journey states")
            for state, action in zip(states, plan.actions, strict=False):
                if state["state_id"] != action.state_id or state["kind"] != action.kind:
                    raise ValueError("Unexpected journey state")
                if type(state["success"]) is not bool or len(state["dom"]["nodes"]) > 100:
                    raise ValueError("Invalid bounded DOM evidence")
            if contains_known_credential(record, self.executor.credential_registry()):
                raise ValueError("Browser DOM contains configured credentials")
            limit = 4 if plan.reference_id else 5
            if len(encoded) > limit:
                raise ValueError("Journey image count exceeded")
            screenshots = []
            total = 0
            captures = {action.state_id for action in plan.actions if action.screenshot}
            for item in encoded:
                if item["state_id"] not in captures:
                    raise ValueError("Screenshot does not match a declared state")
                screenshot = base64.b64decode(item["data"], validate=True)
                total += len(screenshot)
                if (
                    not screenshot.startswith(b"\x89PNG\r\n\x1a\n")
                    or len(screenshot) > config.browser.max_screenshot_bytes
                    or total > 6_000_000
                ):
                    raise ValueError("Journey screenshot exceeded bounds")
                screenshots.append((item["state_id"], screenshot))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("Browser journey returned invalid or private bounded evidence") from exc
        images = []
        for state_id, screenshot in screenshots:
            evidence_id = uuid.uuid4().hex
            image_path = self.directory / (evidence_id + ".png")
            fd = os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as image:
                image.write(screenshot)
            metadata = {
                "evidence_id": evidence_id,
                "journey_id": plan.journey_id,
                "state_id": state_id,
                "viewport": record["viewport"],
                "theme": plan.theme,
                "zoom": plan.zoom,
                "screenshot_sha256": hashlib.sha256(screenshot).hexdigest(),
                "artifact": {"id": evidence_id, "filename": image_path.name, "media_type": "image/png"},
            }
            images.append(EvidenceImage(screenshot, metadata))
        observed = {state["state_id"] for state in states if state["success"]}
        for state in states:
            state["dom"].pop("body_text", None)
        while len(json.dumps(record)) > 30_000:
            candidate = max(states, key=lambda state: len(state["dom"]["nodes"]), default=None)
            if candidate is None or not candidate["dom"]["nodes"]:
                raise ValueError("Journey DOM evidence exceeds the bounded model response")
            candidate["dom"]["nodes"].pop()
            candidate["dom"]["truncated"] = True
        record.update(
            expected_states=[state.model_dump(mode="json") for state in plan.expected_states],
            not_reviewed=[state.id for state in plan.expected_states if state.id not in observed],
            reference_status="reference_not_reviewed",
            images=[image.metadata for image in images],
        )
        return JourneyEvidence(record, images)

    async def rasterize_reference(
        self,
        run_id: str,
        workspace_root: Path,
        svg: str,
        width: int,
        height: int,
        *,
        theme: str = "light",
        zoom: int = 100,
        task_id: str = "implement",
    ) -> bytes:
        from firefly_forge.product_fonts import manrope_style

        validate_browser_request("/", width, height, theme)
        if type(zoom) is not int or not 50 <= zoom <= 200:
            raise ValueError("Reference zoom exceeds the supported bounds")
        validate_reference_svg(svg)
        await self.executor.store.assert_ownership()
        run = await self.executor.store.get_run(run_id)
        config = validate_sandbox_snapshot(run["sandbox_snapshot"])
        if config.mode != "docker" or config.browser is None:
            raise ValueError("Reference raster requires the frozen isolated browser profile")
        if width > config.browser.max_width or height > config.browser.max_height:
            raise ValueError("Reference viewport exceeds its frozen bounds")
        font = manrope_style()
        identity = {
            "run_id": run_id,
            "source_sha256": hashlib.sha256(svg.encode()).hexdigest(),
            "width": width,
            "height": height,
            "theme": theme,
            "zoom": zoom,
            "image": config.browser.image,
            "font_sha256": hashlib.sha256(font.encode()).hexdigest(),
            "renderer": 1,
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        image_path, metadata_path = (
            self.directory / ("reference-" + key + ".png"),
            self.directory / ("reference-" + key + ".json"),
        )
        async with self._reference_locks.setdefault(key, asyncio.Lock()):
            if image_path.exists() or metadata_path.exists():
                try:
                    cached = json.loads(_private_read(metadata_path, 4096))
                    image = _private_read(image_path, config.browser.max_screenshot_bytes)
                    if cached["identity"] != identity or cached["sha256"] != hashlib.sha256(image).hexdigest():
                        raise ValueError("Reference cache hash differs")
                    _reference_png(image, width, height, config.browser.max_screenshot_bytes)
                    return image
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise ValueError("Reference cache is incomplete or changed; no unreviewed image admitted") from exc
            payload = {
                "svg": svg,
                "font": font,
                "width": width,
                "height": height,
                "theme": theme,
                "zoom": zoom,
                "max_bytes": config.browser.max_screenshot_bytes,
            }
            result = await self.executor.execute(
                run_id,
                workspace_root,
                workspace_root,
                ["node", "-e", _RASTER_JS],
                60,
                task_id,
                _browser=True,
                _browser_input=json.dumps(payload, separators=(",", ":")).encode(),
            )
            if result.exit_code:
                raise ValueError("Isolated approved-reference raster failed")
            try:
                record = json.loads(result.output)
                if (record["width"], record["height"]) != (width, height):
                    raise ValueError("Raster viewport differs")
                image = base64.b64decode(record["image"], validate=True)
                _reference_png(image, width, height, config.browser.max_screenshot_bytes)
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError("Reference runner returned invalid PNG evidence") from exc
            for path, data in (
                (image_path, image),
                (
                    metadata_path,
                    json.dumps({"identity": identity, "sha256": hashlib.sha256(image).hexdigest()}).encode(),
                ),
            ):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as target:
                    target.write(data)
            return image
