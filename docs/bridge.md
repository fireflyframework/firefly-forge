<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect an authorized internal agent bridge

Firefly Agent Bridge is an internal service for authorized Firefly developers.
Its source and installation instructions live in the private
[Firefly Agent Bridge repository](https://github.com/fireflyframework/firefly-agent-bridge).
Access to this public Forge repository does not grant access to that service.

Forge includes a client connection to an OpenAI-compatible bridge endpoint.
The bridge runs separately and authenticates its own clients. Configure the
Forge server with the endpoint and an authorized bridge token:

```sh
export FORGE_BRIDGE_URL="http://127.0.0.1:8766"
export FORGE_BRIDGE_TOKEN="your-authorized-bridge-token"
export FORGE_BRIDGE_MODEL="bridge/default"
```

Use HTTPS for a remote endpoint. Keep the token in the server environment,
not in Studio, Git URLs or generated products. Select **Firefly Agent Bridge**
as the execution agent for a run.

Forge connects Firefly Agentic's workspace tools to the configured compatible
endpoint. The bridge does not need direct access to Forge's local repository.
Its model output still passes through the factory's ordinary generation and
verification stages.

Readiness means the URL and token are configured. A real run is needed to
establish endpoint connectivity, model availability and tool behavior. If the
endpoint rejects a request, inspect the run error and ask the bridge operator
to verify the authorized configuration.

Public installations can use [Codex, Claude Code or native Firefly Agentic](agents.md)
without an internal bridge.
