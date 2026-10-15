# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Versioned, observation-only recipes for an explicitly admitted project runner."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.sandbox_models import SandboxInput, validate_sandbox_snapshot
from firefly_forge.workspace_tools import _private_name


def profile_configuration(snapshot: dict[str, Any] | None) -> SandboxInput:
    if not snapshot or snapshot.get("mode") != "docker":
        raise ValueError("Python profiling requires a frozen isolated Docker runner")
    config = validate_sandbox_snapshot(snapshot)
    if config.workspace_transport != "archive" or config.network != "none" or config.pool_binding is not None:
        raise ValueError("Python profiling requires Docker archive transfer and network none without a pool binding")
    return config


def profile_available(snapshot: dict[str, Any] | None) -> bool:
    try:
        profile_configuration(snapshot)
    except ValueError:
        return False
    return True


class PythonProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recipe: Literal["forge-python-cprofile-v1"] = "forge-python-cprofile-v1"
    script: str = Field(min_length=1, max_length=300)
    args: list[str] = Field(default_factory=list, max_length=16)
    sort: Literal["cumulative", "time"] = "cumulative"
    rows: int = Field(default=20, ge=1, le=100, strict=True)
    timeout: int = Field(default=30, ge=1, le=120, strict=True)

    @field_validator("script")
    @classmethod
    def confined_script(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or "\x00" in value
            or path.suffix != ".py"
            or any(_private_name(part) for part in path.parts)
        ):
            raise ValueError("Profiling requires a confined non-private Python source path")
        return path.as_posix()

    @field_validator("args")
    @classmethod
    def bounded_arguments(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item or len(item) > 256 for item in value) or len(json.dumps(value)) > 4096:
            raise ValueError("Profiling arguments exceed the diagnostic bounds")
        return value


class ProfileReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    workspace_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_key: str = Field(pattern=r"^[a-f0-9]{32}:start$")
    cleanup_key: str = Field(pattern=r"^[a-f0-9]{32}:cleanup$")


class PythonProfileObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recipe: Literal["forge-python-cprofile-v1"] = "forge-python-cprofile-v1"
    observation_only: Literal[True] = True
    script: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image: str
    argv: list[str]
    exit_code: int
    output: str = Field(max_length=16000)
    output_truncated: bool
    report_marker_observed: bool
    receipt: ProfileReceipt | None


def profile_observation(
    request: PythonProfileRequest, source_sha256: str, image: str, result: Any, binding: dict[str, Any]
) -> dict[str, Any]:
    receipt: ProfileReceipt | None
    try:
        receipt = ProfileReceipt.model_validate(binding)
        if (
            receipt.start_key != receipt.execution_id + ":start"
            or receipt.cleanup_key != receipt.execution_id + ":cleanup"
        ):
            receipt = None
    except ValueError:
        receipt = None
    return PythonProfileObservation(
        script=request.script,
        source_sha256=source_sha256,
        image=image,
        argv=result.argv,
        exit_code=result.exit_code,
        output=result.output[:16000],
        output_truncated=len(result.output) > 16000,
        report_marker_observed="FORGE_CPROFILE_V1" in result.output,
        receipt=receipt,
    ).model_dump(mode="json")


_PROFILE_LAUNCHER = """import cProfile,hashlib,json,os,pstats,stat,sys
request=json.loads(sys.argv[1])
parts=request['script'].split('/')
directory=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 for part in parts[:-1]:
  child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
  os.close(directory)
  directory=child
 descriptor=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
 with os.fdopen(descriptor,'rb') as source:
  info=os.fstat(source.fileno())
  if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
   raise ValueError('Profiling requires regular source without links')
  data=source.read(1000001)
finally:
 os.close(directory)
if len(data)>1000000 or hashlib.sha256(data).hexdigest()!=request['source_sha256']:
 raise ValueError('Profiling source changed before execution')
code=compile(data,request['script'],'exec')
sys.dont_write_bytecode=True
sys.argv=[request['script'],*request['args']]
sys.path.insert(0,os.path.dirname(os.path.abspath(request['script'])))
namespace={'__name__':'__main__','__file__':request['script'],'__package__':None,'__spec__':None,'__cached__':None}
profiler=cProfile.Profile()
stream=sys.stdout
try:
 profiler.enable()
 exec(code,namespace)
finally:
 profiler.disable()
 try:
  print('\\nFORGE_CPROFILE_V1',file=stream)
  pstats.Stats(profiler,stream=stream).strip_dirs().sort_stats(request['sort']).print_stats(request['rows'])
 except Exception:
  pass
"""


def build_profile_argv(request: PythonProfileRequest, source_sha256: str) -> list[str]:
    request = PythonProfileRequest.model_validate(request.model_dump(mode="json"))
    if re.fullmatch(r"[a-f0-9]{64}", source_sha256) is None:
        raise ValueError("Profiling requires the observed source SHA256")
    payload = {**request.model_dump(mode="json"), "source_sha256": source_sha256}
    return ["python3", "-I", "-c", _PROFILE_LAUNCHER, json.dumps(payload, separators=(",", ":"))]
