# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Provider-owned application grants with private credentials and fresh repository authorization."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from filelock import AsyncFileLock
from pyfly.container import service

from firefly_forge.configuration import ConfigurationStore, validate_endpoint
from firefly_forge.models import RepositoryInput
from firefly_forge.store import Store

Data = dict[str, Any]
SECRET_FIELDS = {"private_key", "client_secret"}
DEFINITIONS: dict[str, Data] = {
    "github": {
        "name": "GitHub App",
        "auth_type": "github_app",
        "permissions": ["contents:write", "pull_requests:write"],
        "fields": ["app_id", "installation_id", "private_key", "host", "app_slug"],
        "required": {"app_id", "installation_id", "private_key"},
        "defaults": {"host": "https://github.com"},
        "setup": "https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app",
    },
    "gitlab": {
        "name": "GitLab OAuth application",
        "auth_type": "oauth_pkce",
        "permissions": ["api", "read_repository", "write_repository"],
        "fields": ["client_id", "client_secret", "redirect_uri", "host"],
        "required": {"client_id", "redirect_uri"},
        "defaults": {"host": "https://gitlab.com"},
        "setup": "https://docs.gitlab.com/integration/oauth_provider/",
    },
    "bitbucket": {
        "name": "Bitbucket OAuth consumer",
        "auth_type": "oauth_consumer",
        "permissions": ["repository", "repository:write", "pullrequest", "pullrequest:write"],
        "fields": ["client_id", "client_secret", "workspace", "grant_type", "redirect_uri"],
        "required": {"client_id", "client_secret", "workspace"},
        "defaults": {"grant_type": "client_credentials"},
        "setup": "https://support.atlassian.com/bitbucket-cloud/docs/use-oauth-on-bitbucket-cloud/",
    },
}


@service
class SCMConnections:
    def __init__(self, store: Store, directory: Path | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.store = store
        self.private = ConfigurationStore(directory or store.path.parent)
        self.path = self.private.directory / "scm-connections.json"
        self.states_path = self.private.directory / "scm-oauth-states.json"
        self._lock = AsyncFileLock(str(self.private.directory / "scm.lock"))
        self._client = client
        self._tokens: dict[str, tuple[str, float]] = {}
        self._status: dict[str, tuple[str, str]] = {}

    def _definition(self, provider: str) -> Data:
        if provider not in DEFINITIONS:
            raise ValueError("Unknown application connection provider")
        return DEFINITIONS[provider]

    def _record(self, provider: str) -> Data:
        self._definition(provider)
        return dict(self.private._read(self.path).get(provider, {}))

    def _values(self, provider: str) -> dict[str, str]:
        definition = self._definition(provider)
        return {**definition["defaults"], **self._record(provider).get("values", {})}

    def _assert_revision(self, provider: str, revision: str | None) -> None:
        if self._record(provider).get("revision") != revision:
            raise ValueError("Application settings changed; repeat the operation with the current grant")

    def configured(self, provider: str) -> bool:
        if provider not in DEFINITIONS:
            return False
        values = self._values(provider)
        return all(values.get(key) for key in self._definition(provider)["required"])

    def snapshot(self, repository: Data) -> Data:
        provider = repository.get("connection_provider") or repository["provider"]
        application = bool(repository.get("connection_provider")) or self.configured(provider)
        return {
            "mode": "application" if application else "unmanaged_git",
            "provider": provider,
            "application_revision": self._record(provider).get("revision") if application else None,
            "repository_id": repository["id"],
            "remote_repository_id": repository.get("remote_repository_id"),
            "url": repository["url"],
            "default_branch": repository["default_branch"],
        }

    def assert_snapshot(self, repository: Data, snapshot: Data | None) -> None:
        if snapshot is not None and self.snapshot(repository) != snapshot:
            raise ValueError("Source-control application or repository authorization changed after admission")

    def delegation_scope(self, repository: Data) -> Data:
        """Nonsecret configured scope; this local cut is not a provider grant check."""
        from firefly_forge.journal import digest
        from firefly_forge.scm_authority_models import canonical_provider_id, exact_origin

        provider = repository.get("connection_provider")
        if provider not in {"github", "gitlab", "bitbucket"}:
            raise ValueError("Configure a provider application before delegating SCM authority")
        definition, record = self._definition(provider), self._record(provider)
        values = {**definition["defaults"], **record.get("values", {})}
        if not all(values.get(key) for key in definition["required"]):
            raise ValueError("Configure a provider application before delegating SCM authority")
        web_origin = exact_origin(values.get("host", "https://bitbucket.org"))
        api_origin = (
            "https://api.github.com"
            if provider == "github" and web_origin == "https://github.com"
            else "https://api.bitbucket.org"
            if provider == "bitbucket"
            else web_origin
        )
        remote = str(repository.get("remote_repository_id", ""))
        installation = canonical_provider_id("github", values["installation_id"]) if provider == "github" else None
        return {
            "provider": provider,
            "web_origin": web_origin,
            "api_origin": api_origin,
            "connection_revision": record["revision"],
            "connection_scope_digest": digest(
                {
                    "provider": provider,
                    "web_origin": web_origin,
                    "api_origin": api_origin,
                    **{
                        key: values.get(key)
                        for key in ("app_id", "installation_id", "client_id", "workspace", "grant_type")
                    },
                }
            ),
            "installation_id": installation,
            "remote_repository_id": canonical_provider_id(provider, remote),
        }

    def _host(self, provider: str) -> str:
        return self._values(provider).get("host", "https://bitbucket.org")

    def _api(self, provider: str) -> str:
        host = self._host(provider)
        if provider == "github":
            return "https://api.github.com" if host == "https://github.com" else host + "/api/v3"
        return host + "/api/v4" if provider == "gitlab" else "https://api.bitbucket.org/2.0"

    def _summary(self, provider: str) -> Data:
        definition, values = self._definition(provider), self._values(provider)
        configured = self.configured(provider)
        grant = self._record(provider).get("grant", {})
        status = "configured" if configured else "needs_configuration"
        detail = (
            "Application settings saved; test the provider grant"
            if configured
            else "Register the application with the provider and save its settings"
        )
        if (
            configured
            and (provider == "gitlab" or values.get("grant_type") == "authorization_code")
            and not grant.get("refresh_token")
        ):
            status, detail = "needs_authorization", "Authorize this application through the provider"
        status, detail = self._status.get(provider, (status, detail))
        if "refresh_state" in grant:
            status, detail = (
                "needs_authorization",
                "The previous token refresh outcome is uncertain; authorize this application again",
            )
        host = self._host(provider)
        installation = (
            host + "/apps/" + quote(values["app_slug"], safe="") + "/installations/new"
            if provider == "github" and values.get("app_slug")
            else host + "/settings/installations"
            if provider == "github"
            else host + "/-/profile/applications"
            if provider == "gitlab"
            else "https://bitbucket.org/"
            + quote(values.get("workspace", ""), safe="")
            + "/workspace/settings/oauth-consumers"
        )
        return {
            "id": provider,
            "provider": provider,
            "name": definition["name"],
            "auth_type": definition["auth_type"],
            "status": status,
            "detail": detail,
            "setup_url": definition["setup"],
            "installation_url": installation,
            "permissions": definition["permissions"],
            "fields": [
                {
                    "key": key,
                    "label": key.replace("_", " ").capitalize(),
                    "secret": key in SECRET_FIELDS,
                    "required": key in definition["required"],
                    "placeholder": "",
                }
                for key in definition["fields"]
            ],
            "config": {
                key + "_configured" if key in SECRET_FIELDS else key: bool(values.get(key))
                if key in SECRET_FIELDS
                else values.get(key, "")
                for key in definition["fields"]
            },
        }

    def catalog(self) -> list[Data]:
        return [self._summary(provider) for provider in DEFINITIONS]

    def redaction_settings(self) -> dict[str, str]:
        data = self.private._read(self.path)
        settings = {}
        for provider in DEFINITIONS:
            record = data.get(provider, {})
            for field in SECRET_FIELDS:
                value = record.get("values", {}).get(field)
                if isinstance(value, str) and value:
                    settings[f"FORGE_SCM_{provider.upper()}_SECRET_{field.upper()}"] = value
            for field in ("access_token", "refresh_token"):
                value = record.get("grant", {}).get(field)
                if isinstance(value, str) and value:
                    settings[f"FORGE_SCM_{provider.upper()}_{field.upper()}"] = value
        for index, (token, _) in enumerate(self._tokens.values()):
            settings[f"FORGE_SCM_INSTALLATION_TOKEN_{index}"] = token
        return settings

    async def configure(self, provider: str, body: Data) -> Data:
        definition = self._definition(provider)
        if set(body) - {"values", "clear_secrets"}:
            raise ValueError("Unknown application configuration field")
        values = body.get("values", {})
        clear = body.get("clear_secrets", [])
        if (
            not isinstance(values, dict)
            or not isinstance(clear, list)
            or set(values) - set(definition["fields"])
            or set(clear) - (SECRET_FIELDS & set(definition["fields"]))
        ):
            raise ValueError("Unknown application configuration field")
        checked = {}
        for key, value in values.items():
            if not isinstance(value, str) or len(value) > 16000 or "\x00" in value:
                raise ValueError("Application configuration value exceeds its limits")
            if key in SECRET_FIELDS and not value:
                continue
            if key != "private_key" and any(ord(char) < 32 for char in value):
                raise ValueError("Application configuration contains a control character")
            if key in {"app_id", "installation_id"} and value and not value.isdecimal():
                raise ValueError("GitHub App and installation identifiers must be numeric")
            if key in {"workspace", "app_slug"} and value and not all(char.isalnum() or char in "_-" for char in value):
                raise ValueError("Invalid provider application or workspace identifier")
            if key == "host":
                value = validate_endpoint(value)
                parsed = urlsplit(value)
                if parsed.scheme != "https" or parsed.path:
                    raise ValueError("Application host must be an HTTPS origin")
            if key == "redirect_uri" and value:
                value = validate_endpoint(value)
                if urlsplit(value).path != f"/api/v1/scm/connections/{provider}/callback":
                    raise ValueError("Redirect URI must point to this provider's Forge callback")
            if key == "grant_type" and value not in {"client_credentials", "authorization_code"}:
                raise ValueError("Unsupported Bitbucket OAuth grant type")
            if key == "private_key" and value:
                try:
                    parsed_key = serialization.load_pem_private_key(value.encode(), password=None)
                except (ValueError, TypeError):
                    raise ValueError("GitHub requires an unencrypted RSA private key") from None
                if not isinstance(parsed_key, rsa.RSAPrivateKey):
                    raise ValueError("GitHub requires an RSA private key")
            checked[key] = value
        async with self._lock:
            data = self.private._read(self.path)
            record = data.get(provider, {})
            previous = {**definition["defaults"], **record.get("values", {})}
            if (
                provider in {"gitlab", "bitbucket"}
                and previous.get("client_secret")
                and any(checked.get(key, previous.get(key)) != previous.get(key) for key in ("host", "client_id"))
                and "client_secret" not in checked
                and "client_secret" not in clear
            ):
                raise ValueError("A changed OAuth host or client requires an explicitly reviewed client secret")
            merged = {**record.get("values", {}), **checked}
            for key in clear:
                merged[key] = ""
            data[provider] = {"values": merged, "revision": uuid.uuid4().hex}
            self.private._write(self.path, data)
            self._tokens.clear()
            self._status.pop(provider, None)
        return self._summary(provider)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        allowed_statuses = kwargs.pop("allowed_statuses", (200, 201))
        response_limit = kwargs.pop("response_limit", None)

        async def perform(client: httpx.AsyncClient) -> httpx.Response:
            if response_limit is None:
                return await client.request(method, url, timeout=20, follow_redirects=False, **kwargs)
            async with client.stream(method, url, timeout=20, follow_redirects=False, **kwargs) as streamed:
                content = bytearray()
                async for chunk in streamed.aiter_bytes():
                    if len(content) + len(chunk) > response_limit:
                        raise ValueError("Provider reference response exceeds its size limit")
                    content.extend(chunk)
                headers = {
                    key: value
                    for key, value in streamed.headers.items()
                    if key not in {"content-encoding", "content-length"}
                }
                return httpx.Response(
                    streamed.status_code, content=bytes(content), headers=headers, request=streamed.request
                )

        try:
            if self._client is not None:
                response = await perform(self._client)
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                    response = await perform(client)
        except httpx.HTTPError:
            raise ValueError("Provider connection could not be completed") from None
        if response.status_code not in allowed_statuses:
            raise ValueError(f"Provider rejected the application request (HTTP {response.status_code})")
        return response

    async def _github_token(
        self,
        repository_id: str | None = None,
        *,
        issues: bool = False,
        pull_request: bool = False,
        references: bool = False,
        issues_read: bool = False,
    ) -> str:
        record, values = self._record("github"), self._values("github")
        if not self.configured("github"):
            raise ValueError("Configure the GitHub App before connecting repositories")
        if issues_read and (not repository_id or issues or pull_request or references):
            raise ValueError("Use a separate repository-scoped issue-read grant")
        cache_key = (
            str(record["revision"])
            + ":"
            + str(repository_id)
            + (
                ":issues-read"
                if issues_read
                else ":references"
                if references
                else ((":pr" if pull_request else ":issues") if issues else "")
            )
        )
        cached = self._tokens.get(cache_key)
        if cached and cached[1] > time.time() + 90:
            return cached[0]
        current = int(time.time())
        signed = jwt.encode(
            {"iat": current - 60, "exp": current + 540, "iss": values["app_id"]},
            values["private_key"],
            algorithm="RS256",
        )
        permissions = {"contents": "write", "pull_requests": "write"} if repository_id else {"metadata": "read"}
        if issues and repository_id:
            permissions = {"pull_requests" if pull_request else "issues": "write"}
        if references and repository_id:
            permissions = {"contents": "write"}
        if issues_read:
            permissions = {"issues": "read"}
        payload: Data = {"permissions": permissions}
        if repository_id:
            if not repository_id.isdecimal():
                raise ValueError("GitHub repository identifier must be numeric")
            payload["repository_ids"] = [int(repository_id)]
        response = await self._request(
            "POST",
            self._api("github") + "/app/installations/" + values["installation_id"] + "/access_tokens",
            headers={
                "Authorization": "Bearer " + signed,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            json=payload,
        )
        try:
            data = response.json()
            token = data["token"]
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            if not isinstance(token, str) or not token or expires <= time.time():
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError("GitHub returned an invalid installation grant") from None
        self._assert_revision("github", record["revision"])
        self._tokens[cache_key] = (token, expires)
        return token

    async def authorize(self, provider: str) -> Data:
        if provider not in {"gitlab", "bitbucket"} or not self.configured(provider):
            raise ValueError("Configure an OAuth application before authorization")
        values = self._values(provider)
        if not values.get("redirect_uri"):
            raise ValueError("Configure the application's callback redirect URI")
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        query = {
            "client_id": values["client_id"],
            "redirect_uri": values["redirect_uri"],
            "response_type": "code",
            "state": state,
        }
        if provider == "gitlab":
            query.update(
                scope="api read_repository write_repository",
                code_challenge_method="S256",
                code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("="),
            )
        async with self._lock:
            states = {
                key: entry
                for key, entry in self.private._read(self.states_path).items()
                if entry["expires_at"] > time.time()
            }
            if len(states) >= 64:
                raise ValueError("Too many pending OAuth authorizations")
            states[hashlib.sha256(state.encode()).hexdigest()] = {
                "provider": provider,
                "browser": hashlib.sha256(nonce.encode()).hexdigest(),
                "verifier": verifier,
                "expires_at": time.time() + 600,
                "revision": self._record(provider)["revision"],
            }
            self.private._write(self.states_path, states)
        endpoint = self._host(provider) + ("/oauth/authorize" if provider == "gitlab" else "/site/oauth2/authorize")
        return {"authorization_url": endpoint + "?" + urlencode(query), "expires_in": 600, "browser_nonce": nonce}

    async def callback(self, provider: str, state: str, code: str, *, browser_nonce: str) -> Data:
        self._definition(provider)
        if not state or len(state) > 256 or not code or len(code) > 4096 or len(browser_nonce) > 256:
            raise ValueError("Invalid OAuth state or authorization code")
        key = hashlib.sha256(state.encode()).hexdigest()
        async with self._lock:
            states = self.private._read(self.states_path)
            entry = states.get(key)
            if not entry or entry["provider"] != provider or entry["expires_at"] <= time.time():
                raise ValueError("OAuth state is invalid, expired or already used")
            if not secrets.compare_digest(entry["browser"], hashlib.sha256(browser_nonce.encode()).hexdigest()):
                raise ValueError("OAuth state does not belong to this browser")
            if entry["revision"] != self._record(provider).get("revision"):
                raise ValueError("Application settings changed; start a new OAuth authorization")
            del states[key]
            self.private._write(self.states_path, states)
        values = self._values(provider)
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": values["redirect_uri"]}
        if provider == "gitlab":
            form.update(client_id=values["client_id"], code_verifier=entry["verifier"])
            if values.get("client_secret"):
                form["client_secret"] = values["client_secret"]
        response = await self._request(
            "POST",
            self._host(provider) + ("/oauth/token" if provider == "gitlab" else "/site/oauth2/access_token"),
            data=form,
            **({"auth": (values["client_id"], values["client_secret"])} if provider == "bitbucket" else {}),
        )
        async with self._lock:
            data = self.private._read(self.path)
            if data[provider]["revision"] != entry["revision"]:
                raise ValueError("Application settings changed; start a new OAuth authorization")
            data[provider]["grant"] = self._grant(response)
            self.private._write(self.path, data)
        self._status.pop(provider, None)
        return self._summary(provider)

    def _grant(self, response: httpx.Response) -> Data:
        try:
            data = response.json()
            token, duration = data["access_token"], int(data["expires_in"])
            refresh, scope = data.get("refresh_token", ""), data.get("scope", "")
            if (
                not isinstance(token, str)
                or not token
                or duration <= 0
                or not isinstance(refresh, str)
                or not isinstance(scope, str)
            ):
                raise ValueError
            return {
                "access_token": token,
                "refresh_token": refresh,
                "expires_at": time.time() + duration,
                "scope": scope,
            }
        except (KeyError, TypeError, ValueError):
            raise ValueError("Provider returned an invalid OAuth grant") from None

    async def _oauth_token(self, provider: str) -> str:
        if not self.configured(provider):
            raise ValueError("Configure the provider's OAuth application first")
        async with self._lock:
            data = self.private._read(self.path)
            record, values = data[provider], self._values(provider)
            grant = record.get("grant", {})
            if "refresh_state" in grant:
                raise ValueError("The previous token refresh outcome is uncertain; authorize the application again")
            if grant.get("access_token") and grant.get("expires_at", 0) > time.time() + 90:
                if provider == "bitbucket" and not {"repository:write", "pullrequest:write"} <= set(
                    str(grant.get("scope", "")).split()
                ):
                    raise ValueError("Bitbucket consumer needs repository:write and pullrequest:write permissions")
                return str(grant["access_token"])
            refresh = grant.get("refresh_token")
            if provider == "gitlab" or values.get("grant_type") == "authorization_code":
                if not refresh:
                    raise ValueError("Authorize this application through the provider first")
                form = {"grant_type": "refresh_token", "refresh_token": refresh}
            else:
                form = {"grant_type": "client_credentials"}
            if provider == "gitlab":
                form.update(client_id=values["client_id"], redirect_uri=values["redirect_uri"])
                if values.get("client_secret"):
                    form["client_secret"] = values["client_secret"]
            if form["grant_type"] == "refresh_token":
                grant["refresh_state"] = "dispatching"
                self.private._write(self.path, data)
            response = await self._request(
                "POST",
                self._host(provider) + ("/oauth/token" if provider == "gitlab" else "/site/oauth2/access_token"),
                data=form,
                **({"auth": (values["client_id"], values["client_secret"])} if provider == "bitbucket" else {}),
            )
            record["grant"] = self._grant(response)
            if provider == "bitbucket":
                scope = set(str(record["grant"]["scope"]).split())
                if not {"repository:write", "pullrequest:write"} <= scope:
                    raise ValueError("Bitbucket consumer needs repository:write and pullrequest:write permissions")
            self.private._write(self.path, data)
            return str(record["grant"]["access_token"])

    def _page_url(self, provider: str, url: str) -> str:
        parsed, allowed = urlsplit(url), urlsplit(self._api(provider))
        if (
            (parsed.scheme, parsed.netloc) != (allowed.scheme, allowed.netloc)
            or parsed.username
            or parsed.password
            or not parsed.path.startswith(allowed.path + "/")
        ):
            raise ValueError("Provider pagination URL has a different origin or API path")
        return url

    def _repository(self, provider: str, data: Data) -> Data:
        if provider == "github":
            identifier, name, url, branch = (
                str(data["id"]),
                data["full_name"],
                data["clone_url"],
                data.get("default_branch"),
            )
        elif provider == "gitlab":
            identifier, name, url, branch = (
                str(data["id"]),
                data["path_with_namespace"],
                data["http_url_to_repo"],
                data.get("default_branch"),
            )
        else:
            identifier, name, branch = data["uuid"], data["full_name"], (data.get("mainbranch") or {}).get("name")
            url = next((link["href"] for link in data["links"]["clone"] if link["name"] == "https"), "")
        if not branch:
            raise ValueError("Provider repository has no default branch; initialize it before connecting")
        parsed, host = urlsplit(url), urlsplit(self._host(provider))
        if (
            (parsed.scheme, parsed.netloc) != (host.scheme, host.netloc)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Provider repository clone URL has an invalid origin")
        try:
            validated = RepositoryInput.model_validate(
                {"name": name, "url": url, "provider": provider, "default_branch": branch}
            )
        except ValueError:
            raise ValueError("Provider returned an invalid repository contract") from None
        return {"id": identifier, **validated.model_dump(), "provider": provider}

    async def repositories(self, provider: str) -> list[Data]:
        self._definition(provider)
        revision = self._record(provider).get("revision")
        token = await self._github_token() if provider == "github" else await self._oauth_token(provider)
        self._assert_revision(provider, revision)
        base = self._api(provider)
        path = (
            "/installation/repositories?per_page=100"
            if provider == "github"
            else "/projects?membership=true&min_access_level=30&per_page=100&archived=false"
            if provider == "gitlab"
            else "/repositories/"
            + quote(self._values(provider)["workspace"], safe="")
            + "?pagelen=100&role=contributor"
        )
        url: str | None = base + path
        result: list[Data] = []
        visited: set[str] = set()
        for _ in range(20):
            if not url:
                break
            if url in visited:
                raise ValueError("Provider pagination repeats a page")
            visited.add(url)
            response = await self._request(
                "GET",
                self._page_url(provider, url),
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json" if provider == "github" else "application/json",
                },
            )
            try:
                payload = response.json()
                entries = (
                    payload["repositories"]
                    if provider == "github"
                    else payload
                    if provider == "gitlab"
                    else payload["values"]
                )
                if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
                    raise ValueError("Provider returned an invalid repository grant")
                result.extend(
                    self._repository(provider, item)
                    for item in entries
                    if not item.get("archived") and not item.get("disabled")
                )
            except (AttributeError, KeyError, TypeError, StopIteration):
                raise ValueError("Provider returned an invalid repository grant") from None
            self._assert_revision(provider, revision)
            url = response.links.get("next", {}).get("url") if provider != "bitbucket" else payload.get("next")
            if provider == "gitlab" and not url and response.headers.get("X-Next-Page"):
                page = response.headers["X-Next-Page"]
                if not page.isdecimal():
                    raise ValueError("Provider returned invalid pagination")
                url = base + path + "&page=" + page
        else:
            if url:
                raise ValueError("Provider grants exceed the 2000-repository discovery limit")
        return result

    async def test(self, provider: str) -> Data:
        self._definition(provider)
        try:
            granted = await self.repositories(provider)
            self._status[provider] = (
                "connected",
                f"Provider accepted this application; {len(granted)} repositories available",
            )
        except ValueError as error:
            self._status[provider] = ("error", str(error))
        return self._summary(provider)

    async def connect(self, provider: str, repository_id: str) -> Data:
        found = next(
            (repository for repository in await self.repositories(provider) if repository["id"] == repository_id), None
        )
        if found is None:
            raise ValueError("Repository is outside the application's current grants")
        for existing in await self.store.repositories():
            if (
                existing.get("connection_provider") == provider
                and existing.get("remote_repository_id") == repository_id
            ):
                return existing
        return await self.store.add_repository(
            RepositoryInput.model_validate(
                {
                    "name": found["name"],
                    "url": found["url"],
                    "provider": provider,
                    "default_branch": found["default_branch"],
                    "connection_provider": provider,
                    "remote_repository_id": repository_id,
                }
            )
        )

    async def authentication(self, repository: Data) -> tuple[str, str]:
        provider = str(repository.get("connection_provider") or repository["provider"])
        grants = await self.repositories(provider)
        found = next(
            (
                grant
                for grant in grants
                if grant["url"] == repository["url"]
                and (not repository.get("remote_repository_id") or grant["id"] == repository["remote_repository_id"])
            ),
            None,
        )
        if found is None:
            raise ValueError("Repository URL and identity do not match the application's current grants")
        token = (
            await self._github_token(str(found["id"])) if provider == "github" else await self._oauth_token(provider)
        )
        return token, self._api(provider)

    async def issue_authentication(self, repository: Data, *, pull_request: bool = False) -> tuple[str, str]:
        """Revalidate the connected remote before obtaining an operation-scoped issue grant."""
        provider = str(repository.get("connection_provider"))
        grants = await self.repositories(provider)
        if not any(
            grant["url"] == repository["url"]
            and grant["id"] == repository.get("remote_repository_id")
            and grant["name"] == repository["name"]
            for grant in grants
        ):
            raise ValueError("Repository URL and identity do not match the application's current grants")
        token = (
            await self._github_token(str(repository["remote_repository_id"]), issues=True, pull_request=pull_request)
            if provider == "github"
            else await self._oauth_token(provider)
        )
        return token, self._api(provider)

    async def github_poll_read(
        self,
        repository: Data,
        resource: str,
        *,
        page: int = 1,
        value: str = "",
    ) -> httpx.Response:
        """Read one pinned API resource with a distinct singleton issue-read grant."""
        if repository.get("connection_provider") != "github":
            raise ValueError("Authenticated polling currently supports GitHub only")
        from firefly_forge.scm import coordinates

        revision = self._record("github").get("revision")
        grants = await self.repositories("github")
        if not any(
            item["id"] == repository.get("remote_repository_id")
            and item["url"] == repository["url"]
            and item["name"] == repository["name"]
            for item in grants
        ):
            raise ValueError("The GitHub App no longer grants this exact repository")
        _, _, name = coordinates(repository["url"])
        if name != repository["name"] or len(name.split("/")) != 2:
            raise ValueError("The connected GitHub repository path changed")
        base = self._api("github") + "/repos/" + name
        params: Data = {}
        if resource in {"issues", "comments"}:
            if type(page) is not int or not 1 <= page <= 20:
                raise ValueError("Use a bounded polling page")
            path = "/issues" + ("/comments" if resource == "comments" else "")
            params = {"sort": "created", "direction": "asc", "per_page": 50, "page": page}
            if resource == "issues":
                params["state"] = "all"
        elif resource in {"issue", "comment"} and value.isdecimal() and int(value) > 0:
            path = "/issues/" + ("comments/" if resource == "comment" else "") + value
        elif (
            resource == "permission"
            and value
            and len(value) <= 100
            and all(character.isascii() and (character.isalnum() or character == "-") for character in value)
        ):
            path = "/collaborators/" + value + "/permission"
        else:
            raise ValueError("Unsupported authenticated polling resource")
        token = await self._github_token(str(repository["remote_repository_id"]), issues_read=True)
        self._assert_revision("github", revision)
        response = await self._request(
            "GET",
            base + path,
            params=params,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            response_limit=4 * 1024 * 1024,
            allowed_statuses=(200, 403, 404, 429),
        )
        self._assert_revision("github", revision)
        return response

    async def reference_authentication(self, repository: Data) -> tuple[str, str]:
        provider = str(repository.get("connection_provider"))
        grants = await self.repositories(provider)
        if not any(
            grant["url"] == repository["url"]
            and grant["id"] == repository.get("remote_repository_id")
            and grant["name"] == repository["name"]
            for grant in grants
        ):
            raise ValueError("Repository URL and identity do not match the application's current grants")
        token = (
            await self._github_token(str(repository["remote_repository_id"]), references=True)
            if provider == "github"
            else await self._oauth_token(provider)
        )
        return token, self._api(provider)

    async def git_environment(self, repository: Data) -> dict[str, str]:
        token, _ = await self.authentication(repository)
        provider = repository.get("connection_provider") or repository["provider"]
        return {
            "FORGE_GIT_USERNAME": {"github": "x-access-token", "gitlab": "oauth2", "bitbucket": "x-token-auth"}[
                provider
            ],
            "FORGE_GIT_PASSWORD": token,
            "FORGE_SCM_TOKEN": token,
            "FORGE_GIT_ORIGIN": urlsplit(repository["url"]).scheme + "://" + urlsplit(repository["url"]).netloc,
            "FORGE_GIT_REPOSITORY_URL": repository["url"],
        }
