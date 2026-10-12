<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect an authorized internal agent bridge

Firefly Agent Bridge is an internal service for authorized Firefly developers.
Its source and installation instructions live in the private
[Firefly Agent Bridge repository](https://github.com/fireflyframework/firefly-agent-bridge).
Access to this public forge repository does not grant access to that service.

forge includes a client connection to an OpenAI-compatible bridge endpoint.
The bridge runs separately and authenticates its own clients. Configure the
forge server with the endpoint and an authorized bridge token:

```sh
export FORGE_BRIDGE_URL="http://127.0.0.1:8766"
export FORGE_BRIDGE_TOKEN="your-authorized-bridge-token"
export FORGE_BRIDGE_MODEL="bridge/default"
```

Use HTTPS for a remote endpoint. Keep the token in the server environment,
or save it through Studio’s write-only provider configuration. Never put it in Git URLs or generated products. Select **Firefly Agent Bridge**
as the execution agent for a run.

forge connects Firefly Agentic's workspace tools to the configured compatible
endpoint. The bridge does not need direct access to forge's local repository.
Its model output still passes through the factory's ordinary generation and
verification stages.

The catalog distinguishes configured access from a successful connection test. The test authenticates a model listing; a real run establishes generation and workspace tool behavior. If the
endpoint rejects a request, inspect the run error and ask the bridge operator
to verify the authorized configuration.

Public installations can use [Codex, Claude Code or native Firefly Agentic](agents.md)
without an internal bridge.
