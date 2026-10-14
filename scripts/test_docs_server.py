# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Exercise the actual loopback transport used by documentation browser checks."""

from __future__ import annotations

import socket
import unittest
from functools import partial
from http.client import HTTPConnection
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from scripts.docs_server import DocumentationServer


class DocumentationServerTests(unittest.TestCase):
    def test_browser_asset_burst_can_connect_before_accept_resumes(self) -> None:
        clients: list[socket.socket] = []
        failures: list[str] = []
        try:
            with DocumentationServer(("127.0.0.1", 0), SimpleHTTPRequestHandler) as server:
                for _ in range(32):
                    client = socket.socket()
                    clients.append(client)
                    client.settimeout(0.1)
                    try:
                        client.connect(server.server_address)
                    except OSError as error:
                        failures.append(type(error).__name__)
                self.assertEqual(failures, [], "Built pages must admit their asset burst without connection resets")
        finally:
            for client in clients:
                client.close()

    def test_native_handler_serves_exact_assets_and_keeps_missing_assets_missing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(b"<h1>Owned documentation</h1>")
            (root / "asset.css").write_bytes(b"body { color: #262821; }")
            with DocumentationServer(
                ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=directory)
            ) as server:
                worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                worker.start()
                try:
                    for path, status, mime, content in [
                        ("/", 200, "text/html", b"<h1>Owned documentation</h1>"),
                        ("/asset.css", 200, "text/css", b"body { color: #262821; }"),
                        ("/missing.png", 404, "text/html", None),
                    ]:
                        with self.subTest(path=path):
                            connection = HTTPConnection(*server.server_address, timeout=2)
                            try:
                                connection.request("GET", path)
                                response = connection.getresponse()
                                body = response.read()
                                self.assertEqual(response.status, status)
                                self.assertIn(mime, response.getheader("Content-Type"))
                                if content is not None:
                                    self.assertEqual(body, content)
                            finally:
                                connection.close()
                finally:
                    server.shutdown()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
