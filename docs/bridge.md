<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect a local agent bridge

Firefly Agent Bridge runs on the machine with your official Codex or Claude Code subscription login. Get its source and installation instructions from the
[Firefly Agent Bridge repository](https://github.com/fireflyframework/firefly-agent-bridge).
Install and operate it separately from Forge, then issue a scoped bridge token for the factory's application connection. Bridge execution is intended for local development and testing; provider account terms and subscription limits still apply.

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

Forge also supports direct [Codex, Claude Code and native Firefly Agentic](agents.md) connections. Choose the provider and execution boundary that meet the project's requirements.

## Verify image input for the selected configuration

Image input requires a successful bounded verification for the bridge adapter's
current configuration and software version. A model listing exposes that
receipt; forge reads it for the conversation's frozen provider. Listing models
does not itself run the verification or invoke a model.

An adapter update can change its configuration revision. Keep historical
receipts with the conversations that used them, and verify the updated adapter
before selecting it for new image requests. A saved image or an earlier
successful answer does not establish current capability. If the bridge is
unavailable, forge reports capability as unverified until it can read the
current receipt again. Restore the connection before repeating a model request.
