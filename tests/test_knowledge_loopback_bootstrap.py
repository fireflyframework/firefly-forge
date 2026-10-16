# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Managed host bootstrap with real in-memory TLS, no sockets or services."""

import hashlib
import ipaddress
import ssl

import pytest
from click.testing import CliRunner
from cryptography import x509

from firefly_forge.cli import main
from firefly_forge.configuration import ConfigurationStore
from firefly_forge.knowledge import knowledge_tls
from firefly_forge.managed_knowledge import bootstrap_knowledge


def fingerprints(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()
    }


def tls_handshake(trust, service, hostname):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(service / "server.crt", service / "server.key")
    client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
    client = trust.wrap_bio(client_in, client_out, server_hostname=hostname)
    peer = server.wrap_bio(server_in, server_out, server_side=True)
    done = set()
    for _ in range(20):
        for name, connection in [("client", client), ("server", peer)]:
            if name not in done:
                try:
                    connection.do_handshake()
                    done.add(name)
                except ssl.SSLWantReadError:
                    pass
        if client_out.pending:
            server_in.write(client_out.read())
        if server_out.pending:
            client_in.write(server_out.read())
        if done == {"client", "server"}:
            return client.version()
    raise AssertionError("In-memory TLS did not finish")


@pytest.mark.parametrize(
    "host,url",
    [
        ("127.0.0.1", "https://127.0.0.1:62665"),
        ("localhost", "https://localhost:62665"),
        ("::1", "https://[::1]:62665"),
    ],
)
def test_host_bootstrap_uses_its_exact_tls_identity_and_keeps_required_knowledge(tmp_path, host, url):
    private, service = ConfigurationStore(tmp_path / "factory"), tmp_path / "service"
    assert bootstrap_knowledge(private, service, endpoint=url)["state"] == "initialized"
    settings = private._read(private.directory / "knowledge.json")["settings"]
    assert settings["endpoint"] == url
    assert all(settings[key] is True for key in ["enabled", "required", "auto_memory"])
    cert = x509.load_pem_x509_certificate((service / "server.crt").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if host == "localhost":
        assert san.get_values_for_type(x509.DNSName) == [host]
    else:
        assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address(host)]
    trust = knowledge_tls(settings["ca_certificate"])
    assert trust.verify_mode == ssl.CERT_REQUIRED and trust.check_hostname
    assert tls_handshake(trust, service, host).startswith("TLS")
    with pytest.raises(ssl.SSLCertVerificationError):
        tls_handshake(trust, service, "another-service")
    with pytest.raises(ssl.SSLCertVerificationError):
        tls_handshake(ssl.create_default_context(), service, host)
    before = fingerprints(tmp_path)
    assert bootstrap_knowledge(private, service)["state"] == "existing"
    assert bootstrap_knowledge(private, service, endpoint=url)["state"] == "existing"
    assert fingerprints(tmp_path) == before


@pytest.mark.parametrize("field", ["endpoint", "api_key", "ca_certificate"])
def test_existing_managed_connection_drift_is_refused_without_rewriting_any_file(tmp_path, field):
    private, service = ConfigurationStore(tmp_path / "factory"), tmp_path / "service"
    bootstrap_knowledge(private, service)
    path = private.directory / "knowledge.json"
    saved = private._read(path)
    saved["settings"][field] = "https://localhost:62665" if field == "endpoint" else "fictional-changed-value"
    private._write(path, saved)
    before = fingerprints(tmp_path)
    with pytest.raises(ValueError, match="connection.*changed"):
        bootstrap_knowledge(private, service)
    assert fingerprints(tmp_path) == before


def test_cli_host_bootstrap_is_explicit_and_endpoint_changes_do_not_rotate_credentials(tmp_path):
    args = [
        "knowledge",
        "bootstrap",
        "--data-dir",
        str(tmp_path / "factory"),
        "--service-dir",
        str(tmp_path / "service"),
    ]
    cli = CliRunner()
    assert cli.invoke(main, [*args, "--endpoint", "https://localhost:62665"]).exit_code == 0
    before = fingerprints(tmp_path)
    changed = cli.invoke(main, [*args, "--endpoint", "https://localhost:62666"])
    assert changed.exit_code != 0
    assert "connection" in changed.output
    assert fingerprints(tmp_path) == before
    assert cli.invoke(main, args).exit_code == 0
    assert fingerprints(tmp_path) == before


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:62665",
        "https://example.invalid:62665",
        "https://localhost.example:62665",
        "https://user@localhost:62665",
        "https://@localhost:62665",
        "https://localhost:62665?",
        "https://localhost:62665#",
        "https://localhost:62665/api",
        "https://localhost:0",
        "https://localhost:65536",
        "https://localhost:",
        "https://localhost:abc",
        "https://localhost:62665\n",
        "https://[::1%eth0]:62665",
    ],
)
def test_host_endpoint_refuses_nonlocal_or_ambiguous_urls_before_creating_service_files(tmp_path, url):
    private = ConfigurationStore(tmp_path / "factory")
    before = fingerprints(tmp_path)
    with pytest.raises(ValueError):
        bootstrap_knowledge(private, tmp_path / "service", endpoint=url)
    assert fingerprints(tmp_path) == before and not (tmp_path / "service").exists()


def test_default_manifest_and_unrelated_saved_flags_keep_the_existing_contract(tmp_path):
    private, service = ConfigurationStore(tmp_path / "factory"), tmp_path / "service"
    bootstrap_knowledge(private, service)
    assert set(private._read(private.directory / "managed-knowledge.json")) == {"version", "files"}
    path = private.directory / "knowledge.json"
    saved = private._read(path)
    saved["settings"].update(required=False, auto_memory=False)
    private._write(path, saved)
    before = fingerprints(tmp_path)
    assert bootstrap_knowledge(private, service)["state"] == "existing"
    assert fingerprints(tmp_path) == before


def test_existing_external_connection_cannot_be_replaced_by_the_host_option(tmp_path):
    private = ConfigurationStore(tmp_path / "factory")
    private._write(
        private.directory / "knowledge.json",
        {
            "revision": "existing",
            "settings": {"endpoint": "https://external.invalid", "api_key": "fictional-existing-credential"},
        },
    )
    assert bootstrap_knowledge(private, tmp_path / "service")["state"] == "external"
    before = fingerprints(tmp_path)
    with pytest.raises(ValueError, match="cannot be changed"):
        bootstrap_knowledge(private, tmp_path / "service", endpoint="https://localhost:62665")
    assert bootstrap_knowledge(private, tmp_path / "service")["state"] == "external"
    assert fingerprints(tmp_path) == before and not (tmp_path / "service").exists()


def test_malformed_saved_credential_is_withheld_without_disclosing_its_value(tmp_path):
    private, service = ConfigurationStore(tmp_path / "factory"), tmp_path / "service"
    bootstrap_knowledge(private, service)
    path = private.directory / "knowledge.json"
    saved = private._read(path)
    canary = "fictional-credential-canary"
    saved["settings"]["api_key"] = {"value": canary}
    private._write(path, saved)
    before = fingerprints(tmp_path)
    with pytest.raises(ValueError) as error:
        bootstrap_knowledge(private, service)
    assert canary not in str(error.value)
    assert fingerprints(tmp_path) == before
