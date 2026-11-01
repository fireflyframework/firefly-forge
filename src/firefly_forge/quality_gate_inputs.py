# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Read approved tool/rule inputs through the existing confined workspace reader."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from firefly_forge.product_provenance import content_digest as digest
from firefly_forge.quality_gate_models import QualityGateContractV1
from firefly_forge.quality_gates import _json
from firefly_forge.source_catalog import SourceCatalogBudget, _Work
from firefly_forge.workspace_tools import WorkspaceTools, _descriptor_path, _source_root_identity, _source_stamp


class QualityRequirementsError(ValueError):
    pass


def _raw_locked_version(ecosystem: str, package: str, content: str) -> str:
    try:
        if ecosystem == "python":

            def canonical(name: str) -> str:
                return re.sub(r"[-_.]+", "-", name).lower()

            entries = tomllib.loads(content).get("package", [])
            versions = [item["version"] for item in entries if canonical(item["name"]) == canonical(package)]
        elif ecosystem == "composer":
            document = _json(content)
            entries = document.get("packages", []) + document.get("packages-dev", [])
            versions = [item["version"] for item in entries if item["name"] == package]
        elif ecosystem == "node":
            document = _json(content)
            item = document["packages"]["node_modules/" + package]
            if item.get("link"):
                raise ValueError("Linked tool packages need an independently attested toolchain")
            versions = [item["version"]]
        else:
            raise ValueError("Unsupported quality-tool lock format")
        if len(versions) != 1 or not isinstance(versions[0], str) or not versions[0].strip():
            raise ValueError("One exact tool version is required")
        return versions[0]
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise QualityRequirementsError("Configure one unambiguous approved tool entry in the component lock") from error


def locked_quality_version(ecosystem: str, package: str, content: str) -> str:
    value = _raw_locked_version(ecosystem, package, content)
    if ecosystem == "composer":
        if (
            re.fullmatch(r"v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:\+[0-9A-Za-z.-]+)?", value)
            is None
        ):
            raise QualityRequirementsError("Configure an exact stable Composer tool version")
        return value.removeprefix("v")
    return value


@dataclass
class _InputFile:
    parent: int
    descriptor: int
    path: Path
    stamp: tuple[int, ...]

    def fence(self) -> None:
        if (
            _descriptor_path(self.parent) != self.path.parent
            or _descriptor_path(self.descriptor) != self.path
            or _source_stamp(os.fstat(self.descriptor)) != self.stamp
            or _source_stamp(os.stat(self.path.name, dir_fd=self.parent, follow_symlinks=False)) != self.stamp
        ):
            raise QualityRequirementsError("Approved quality input metadata changed")

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            os.close(self.parent)
            self.descriptor = self.parent = -1


@dataclass(frozen=True)
class PreparedQualityProjectInputs:
    component: Path
    contract: QualityGateContractV1
    contract_digest: str
    version: str
    raw_version: str
    lock_digest: str
    configuration: dict[str, str]
    _files: tuple[_InputFile, ...] = field(repr=False)
    _root: tuple[int, int, int] = field(repr=False)
    _view: str = field(repr=False)
    _budget: SourceCatalogBudget = field(repr=False)

    @property
    def configuration_digest(self) -> str:
        return digest(self.configuration)

    def _view_digest(self) -> str:
        return digest(
            {
                "component": str(self.component),
                "contract": self.contract.model_dump(mode="json"),
                "contract_digest": self.contract_digest,
                "version": self.version,
                "raw_version": self.raw_version,
                "lock_digest": self.lock_digest,
                "configuration": self.configuration,
            }
        )

    def fence(self) -> None:
        try:
            if self._view_digest() != self._view or _source_root_identity(self.component) != self._root:
                raise QualityRequirementsError("Approved quality inputs or their captured view changed")
            for item in self._files:
                if item.descriptor < 0:
                    raise QualityRequirementsError("Approved quality input capture was closed")
                item.fence()
        except (OSError, ValueError) as error:
            raise QualityRequirementsError("The approved quality input capture is no longer current") from error

    async def assert_current(self) -> None:
        self.fence()
        actual = await prepare_quality_project_inputs(self.component, self.contract, budget=self._budget)
        try:
            if actual._view_digest() != self._view:
                raise QualityRequirementsError("Approved quality inputs changed during verification")
            self.fence()
        finally:
            actual.close()

    def close(self) -> None:
        for item in self._files:
            item.close()

    def __del__(self) -> None:
        self.close()


async def prepare_quality_project_inputs(
    component: Path, contract: QualityGateContractV1, *, budget: SourceCatalogBudget | None = None
) -> PreparedQualityProjectInputs:
    contract = QualityGateContractV1.model_validate(contract.model_dump(mode="json"))
    if component.is_symlink() or not component.is_dir():
        raise QualityRequirementsError("Quality checks require their real selected component directory")
    if contract.tool.approved_version is None:
        raise QualityRequirementsError("Configure an approved quality-tool version before verification")
    tools = WorkspaceTools(component, repository_root=component)
    component = tools.root
    root = _source_root_identity(component)
    selected_budget = budget or SourceCatalogBudget()
    root_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    work = _Work(root_fd, selected_budget)
    files: list[_InputFile] = []

    async def capture(path: str) -> bytes:
        parent = tools._parent_fd(tools._parts(path), create=False)
        descriptor = -1
        try:
            descriptor = os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise QualityRequirementsError("Approved quality inputs require owned regular unlinked files")
            tools._check_descriptor(descriptor)
            item = _InputFile(parent, descriptor, component / path, _source_stamp(info))
            item.fence()
            data = bytearray()
            while True:
                work.tick()
                chunk = os.read(descriptor, selected_budget.chunk_bytes)
                if not chunk:
                    break
                work.source_bytes += len(chunk)
                if (
                    selected_budget.max_source_bytes is not None
                    and work.source_bytes > selected_budget.max_source_bytes
                ):
                    raise QualityRequirementsError("The configured quality input source budget was exceeded")
                data.extend(chunk)
                if len(data) > info.st_size:
                    raise QualityRequirementsError("Approved quality input grew during capture")
                await asyncio.sleep(0)
            if len(data) != info.st_size:
                raise QualityRequirementsError("Approved quality input changed during capture")
            item.fence()
            files.append(item)
            return bytes(data)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)
            raise

    try:
        lock = await capture(contract.tool.lock_file)
        raw_version = _raw_locked_version(contract.tool.ecosystem, contract.tool.package, lock.decode("utf-8"))
        version = locked_quality_version(contract.tool.ecosystem, contract.tool.package, lock.decode("utf-8"))
        if version != contract.tool.approved_version:
            raise QualityRequirementsError("The component lock differs from the approved quality-tool version")
        configuration = {}
        for path in contract.configuration_paths:
            content = await capture(path)
            configuration[path] = hashlib.sha256(content).hexdigest()
        if configuration != contract.approved_configuration:
            raise QualityRequirementsError("Restore the approved quality-rule configuration before verification")
        work.tick()
        prepared = PreparedQualityProjectInputs(
            component,
            contract,
            digest(contract.model_dump(mode="json")),
            version,
            raw_version,
            hashlib.sha256(lock).hexdigest(),
            configuration,
            tuple(files),
            root,
            "",
            selected_budget,
        )
        object.__setattr__(prepared, "_view", prepared._view_digest())
        prepared.fence()
        return prepared
    except BaseException as error:
        for item in files:
            item.close()
        if isinstance(error, (OSError, ValueError)) and not isinstance(error, QualityRequirementsError):
            raise QualityRequirementsError("Prepare the approved tool lock and rule files in this component") from error
        raise
    finally:
        os.close(root_fd)
