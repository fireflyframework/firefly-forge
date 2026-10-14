# Contributing

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

Open an issue describing the behavior and how to reproduce it. Keep changes focused and match existing conventions. Use Python 3.13+, Node.js 22+ and Git.

```sh
uv sync --group dev --group docs
npm --prefix studio ci
make check
make package
```

Add meaningful behavioral tests for lifecycle, API or provider changes. Live provider tests require your own authorized account and are reported separately from deterministic tests. Never commit credentials, dependency directories, generated builds, internal bridge source, private branding implementation or assistant working notes. Update task-based user documentation alongside behavior changes.

See [the contribution guide](docs/contributing.md) for architecture and release conventions.
