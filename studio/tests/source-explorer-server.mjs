/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";
import { createServer } from "vite";

const studio = process.cwd();
const output = resolve(
  process.env.FORGE_SOURCE_FIXTURE_OUTPUT ||
    "test-results/source-explorer-fixture",
);
mkdirSync(output, { recursive: true });
const entry = resolve(output, "source-fixture.tsx");
writeFileSync(
  entry,
  `/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { SourceExplorer } from ${JSON.stringify(resolve(studio, "src/SourceExplorer.tsx"))};
import ${JSON.stringify(resolve(studio, "src/styles.css"))};
document.documentElement.dataset.theme=new URLSearchParams(location.search).get("theme")||"light";
function Fixture(){
  const [run,setRun]=useState("a".repeat(32));
  const [token,setToken]=useState("controlled-source-viewer-token");
  useEffect(()=>{const change=(event)=>{if(typeof event.detail?.runId==="string")setRun(event.detail.runId);if(typeof event.detail?.token==="string")setToken(event.detail.token);};
    window.addEventListener("source-fixture",change);return()=>window.removeEventListener("source-fixture",change);},[]);
  return <main className="source-fixture-shell"><header className="source-fixture-label"><h1>Source archive</h1><p>Controlled browser fixture · no live project</p></header>
    <SourceExplorer connection={{base:"/api/v1",token,authenticated:true}} runId={run}/>
    <footer className="source-fixture-footer">Fixture-only readback · no source or execution changes</footer></main>;
}
createRoot(document.getElementById("root")).render(<Fixture/>);
`,
);
const html = `<!doctype html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Controlled source explorer fixture</title><style>.source-fixture-shell{max-width:1120px;margin:32px auto;padding:0 24px}.source-fixture-label{padding-bottom:20px;border-bottom:1px solid var(--line)}.source-fixture-label h1{font-size:24px;margin:0 0 8px}.source-fixture-label p,.source-fixture-footer{font-size:13px;color:var(--muted)}.source-fixture-footer{border-top:1px solid var(--line);padding:20px 0;margin-top:28px}@media(max-width:600px){.source-fixture-shell{margin:20px auto;padding:0 16px}}</style></head>
<body><div id="root"></div><script type="module" src=${JSON.stringify("/@fs/" + entry)}></script></body></html>`;
const server = await createServer({
  root: studio,
  configFile: resolve(studio, "vite.config.ts"),
  resolve: { dedupe: ["react", "react-dom"] },
  server: {
    host: "127.0.0.1",
    port: 5197,
    strictPort: true,
    fs: { allow: [resolve(studio, ".."), output] },
  },
  plugins: [
    {
      name: "controlled-source-fixture",
      configureServer(current) {
        current.middlewares.use(async (req, res, next) => {
          if (!req.url?.startsWith("/__source_explorer__")) return next();
          res.setHeader("Content-Type", "text/html");
          res.end(await current.transformIndexHtml(req.url, html));
        });
      },
    },
  ],
});
await server.listen();
for (const signal of ["SIGINT", "SIGTERM"])
  process.once(signal, async () => {
    await server.close();
    process.exit(0);
  });
