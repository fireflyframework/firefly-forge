# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Product checks validate installed version and real API exports after generation."""

from __future__ import annotations

from typing import Any

from firefly_forge.framework_knowledge_models import framework_version

_PYFLY_CHECK = """import ast, importlib, importlib.metadata, os, pathlib, sys
expected = tuple(map(int,sys.argv[1].split('.')))
actual = tuple(map(int,importlib.metadata.version('pyfly').split('.')))
if actual != expected:
    raise RuntimeError('Installed pyfly version differs from the retained framework target')
distribution = pathlib.Path(importlib.metadata.distribution('pyfly').locate_file('pyfly')).resolve()
def selected(name):
    module = importlib.import_module(name)
    locations = ([pathlib.Path(module.__file__).resolve()] if getattr(module,'__file__',None)
                 else [pathlib.Path(p).resolve() for p in module.__path__])
    if not locations or any(not p.is_relative_to(distribution) for p in locations):
        raise RuntimeError('pyfly imports must resolve to the selected installed framework')
    return module
for directory, children, files in os.walk('.'):
    children[:] = [p for p in children
                   if p not in {'.git','.venv','node_modules','vendor','__pycache__'}
                   and not pathlib.Path(directory,p).is_symlink()]
    for name in files:
        path = pathlib.Path(directory,name)
        if path.suffix != '.py' or path.is_symlink():
            continue
        tree = ast.parse(path.read_text(encoding='utf8'),filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                for item in node.names:
                    if item.name == 'pyfly' or item.name.startswith('pyfly.'):
                        selected(item.name)
            elif (isinstance(node,ast.ImportFrom) and node.module and not node.level
                  and (node.module == 'pyfly' or node.module.startswith('pyfly.'))):
                module = selected(node.module)
                for item in node.names:
                    if item.name == '*' or hasattr(module,item.name):
                        continue
                    try:
                        selected(node.module+'.'+item.name)
                    except ImportError as error:
                        raise RuntimeError('Unknown installed pyfly API export: '+node.module+'.'+item.name) from error
print('Installed pyfly version and source API imports match the retained target')
"""

_LARAFLY_CHECK = r"""require 'vendor/autoload.php';
$version = \Composer\InstalledVersions::getPrettyVersion('fireflyframework/larafly');
$normalize = static fn($value) => implode('.',array_map('intval',explode('.',ltrim($value,'v'))));
if ($normalize($version) !== $normalize($argv[1])) {
    throw new \RuntimeException('Installed larafly version differs from the retained framework target');
}
echo "Installed larafly version matches the retained target\n";
"""


def conformance_commands(pin: dict[str, Any]) -> list[list[str]]:
    version = framework_version(pin["framework_version"])
    if pin["framework"] == "pyfly":
        return [["uv", "run", "python", "-c", _PYFLY_CHECK, version]]
    if pin["framework"] == "larafly":
        return [["php", "-r", _LARAFLY_CHECK, version], ["vendor/bin/phpstan", "analyse", "--no-progress"]]
    raise ValueError("Framework conformance requires a selected official framework")


def runtime_probe_command(requirements: dict[str, Any]) -> list[str]:
    if requirements["runtime"] == "php":
        return ["php", "-r", "echo json_encode(['runtime'=>'php','version'=>PHP_VERSION]);"]
    return [
        "uv",
        "run",
        "--offline",
        "--no-project",
        "--python",
        ">=3.13",
        "python",
        "-I",
        "-c",
        'import json,sys; print(json.dumps({"runtime":"python","version":".".join(map(str,sys.version_info[:3]))}))',
    ]


async def attest_runtime(engine: Any, run: dict[str, Any], workspace: Any, component: Any, task_key: str) -> None:
    import hashlib
    import json
    import re

    from pyfly.data.relational.sqlalchemy import LockMode

    from firefly_forge.framework_knowledge_requirements import runtime_satisfies
    from firefly_forge.product_provenance import content_digest as digest
    from firefly_forge.project_access import ProjectAccess
    from firefly_forge.store import identifier

    pin = run["framework_knowledge_pin"]
    requirements = pin["runtime_requirements"]
    if engine.sandbox is None:
        raise ValueError("A native sandbox is required to attest the selected framework runtime before model use")
    command = runtime_probe_command(requirements)
    captured: dict[str, Any] = {}
    result = await engine.sandbox.execute(
        run["id"], workspace, component, command, 60, "implement", _receipt_sink=captured.update
    )
    if result.exit_code or len(result.output) > 4096:
        raise ValueError("Configure the selected framework interpreter prerequisite before model use")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(dict(pairs)) != len(pairs):
            raise ValueError("Runtime prerequisite response contains duplicate metadata")
        return dict(pairs)

    try:
        observed = json.loads(result.output, object_pairs_hook=unique)
        if (
            set(observed) != {"runtime", "version"}
            or observed["runtime"] != requirements["runtime"]
            or not isinstance(observed["version"], str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", observed["version"])
        ):
            raise ValueError("Runtime prerequisite response is invalid")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Runtime prerequisite response is invalid") from exc
    if not runtime_satisfies(requirements, observed["version"]) or (
        observed["runtime"] == "python" and tuple(map(int, observed["version"].split("."))) < (3, 13, 0)
    ):
        raise ValueError(
            "Selected runtime does not satisfy the immutable framework prerequisite: "
            + requirements["runtime_requirement"]
        )
    if not captured.get("execution_id"):
        raise ValueError("Framework runtime prerequisite lacks a canonical native command receipt")
    receipt = {
        "version": 1,
        "run_id": run["id"],
        "task_key": task_key,
        "pin_digest": pin["digest"],
        "requirements_digest": requirements["digest"],
        "observed": observed,
        "command": captured,
        "argv_digest": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest(),
    }
    async with engine.store._unit():
        row = await engine.store.run_records.find_by_id(identifier(run["id"]), lock=LockMode.PESSIMISTIC_WRITE)
        if row is None or row.data.get("framework_knowledge_pin") != pin:
            raise ValueError("Framework target changed while attesting its runtime prerequisite")
        await ProjectAccess(engine.store).authorize_execution(row.data)
        event = await engine.journal._append(
            run["id"], "implement", digest(receipt), "framework_runtime_attestation", receipt
        )
        row.data = {**row.data, "framework_runtime_attestation": {**receipt, "event_digest": event["digest"]}}
        await engine.store.run_records.save(row)
