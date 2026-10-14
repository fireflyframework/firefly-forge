# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Serve built documentation on loopback for native browser qualification."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class DocumentationServer(ThreadingHTTPServer):
    # Browser galleries request many assets before the serving thread accepts them.
    request_queue_size = 128


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    if not directory.is_dir():
        parser.error("Documentation directory must be a directory")
    with DocumentationServer(
        ("127.0.0.1", args.port), partial(SimpleHTTPRequestHandler, directory=str(directory))
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
