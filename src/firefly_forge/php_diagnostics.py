# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Explicit, bounded PHP observations through an admitted isolated command runner."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from firefly_forge.diagnostic_recipes import ProfileReceipt, PythonProfileRequest
from firefly_forge.diagnostic_recipes import profile_configuration as _profile_configuration
from firefly_forge.sandbox_models import SandboxInput
from firefly_forge.workspace_tools import _private_name


def profile_configuration(snapshot: dict[str, Any] | None) -> SandboxInput:
    return _profile_configuration(snapshot, "PHP")


class PHPProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    recipe: Literal["forge-php-runtime-v1", "forge-php-xdebug-v1"] = "forge-php-runtime-v1"
    script: str = Field(min_length=1, max_length=300)
    args: list[str] = Field(default_factory=list, max_length=16)
    rows: int = Field(default=20, ge=1, le=100)
    timeout: int = Field(default=30, ge=1, le=120)

    @field_validator("script")
    @classmethod
    def confined_script(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in value
            or "\x00" in value
            or (path.suffix != ".php" and path.name != "artisan")
            or any(_private_name(part) for part in path.parts)
        ):
            raise ValueError("Profiling requires a confined non-private PHP source or artisan entrypoint")
        return path.as_posix()

    @field_validator("args")
    @classmethod
    def bounded_arguments(cls, value: list[str]) -> list[str]:
        return PythonProfileRequest.bounded_arguments(value)


class PHPProfileObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    recipe: Literal["forge-php-runtime-v1", "forge-php-xdebug-v1"]
    observation_only: Literal[True] = True
    best_effort: Literal[True] = True
    script: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    image: str
    argv: list[str]
    exit_code: int
    output: str = Field(max_length=16000)
    output_truncated: bool
    report_marker_observed: bool
    receipt: ProfileReceipt | None


def php_profile_observation(
    request: PHPProfileRequest, source_sha256: str, image: str, result: Any, binding: dict[str, Any]
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
    return PHPProfileObservation(
        recipe=request.recipe,
        script=request.script,
        source_sha256=source_sha256,
        image=image,
        argv=result.argv,
        exit_code=result.exit_code,
        output=result.output[:16000],
        output_truncated=len(result.output) > 16000,
        report_marker_observed="FORGE_PHP_PROFILE_V1" in result.output,
        receipt=receipt,
    ).model_dump(mode="json")


_SOURCE_GUARD = r"""static function(string $script, string $sha): void {
    $path = '.';
    $parts = explode('/', $script);
    foreach ($parts as $index => $part) {
        $path .= '/' . $part;
        $info = lstat($path);
        $kind = $index === count($parts) - 1 ? 0100000 : 0040000;
        if ($info === false || ($info['mode'] & 0170000) !== $kind) {
            throw new RuntimeException('Profiling source must be regular and unlinked');
        }
    }
    if ($info['nlink'] !== 1) { throw new RuntimeException('Profiling source must not have hard links'); }
    $stream = fopen($path, 'rb');
    if ($stream === false) { throw new RuntimeException('Profiling source is unavailable'); }
    try {
        $opened = fstat($stream);
        $data = fread($stream, 1000001);
        if ($opened === false || $opened['ino'] !== $info['ino'] || $opened['dev'] !== $info['dev']
            || $opened['nlink'] !== 1 || $data === false || strlen($data) > 1000000
            || hash('sha256', $data) !== $sha) {
            throw new RuntimeException('Profiling source changed before execution');
        }
    } finally { fclose($stream); }
}"""


_CACHEGRIND_PARSER = r"""
function forge_cachegrind(string $file, int $limit): array {
    $info = lstat($file);
    if ($info === false || ($info['mode'] & 0170000) !== 0100000 || $info['nlink'] !== 1
        || $info['size'] > 8388608 || $limit < 1 || $limit > 100) {
        throw new RuntimeException('Profile file exceeds its bound or is not regular');
    }
    $stream = fopen($file, 'rb');
    if ($stream === false) { throw new RuntimeException('Profile file unavailable'); }
    $files = []; $names = []; $rows = []; $events = null; $summary = false; $bytes = 0;
    $currentFile = ''; $current = null; $callee = null; $calleeFile = ''; $edge = false;
    $resolve = static function(string $value, array &$table): string {
        if (strlen($value) > 1024) { throw new RuntimeException('Profile label exceeds its bound'); }
        if (preg_match('/^\(([0-9]{1,8})\)(?: (.*))?$/D', $value, $match)) {
            if (isset($match[2])) { $table[$match[1]] = $match[2]; }
            if (!isset($table[$match[1]])) { throw new RuntimeException('Unknown profile label reference'); }
            $value = $table[$match[1]];
        }
        if (count($table) > 4096) { throw new RuntimeException('Profile label table exceeds its bound'); }
        return $value;
    };
    $entry = static function(string $file, string $name) use (&$rows): string {
        $key = $file . "\0" . $name;
        if (!isset($rows[$key])) {
            if (count($rows) >= 4096) { throw new RuntimeException('Profile functions exceed their bound'); }
            $rows[$key] = ['file' => $file, 'function' => $name, 'self_time' => 0, 'self_memory' => 0, 'calls' => 0];
        }
        return $key;
    };
    $add = static fn(int $a, int $b): int => (int)max(-9007199254740991, min(9007199254740991, $a + $b));
    try {
        while (($raw = fgets($stream, 2049)) !== false) {
            $bytes += strlen($raw);
            if ($bytes > 8388608) { throw new RuntimeException('Profile exceeds its read bound'); }
            if (!str_ends_with($raw, "\n") && !feof($stream)) {
                throw new RuntimeException('Profile line exceeds its bound');
            }
            $line = trim($raw);
            if ($summary && $line !== '') { throw new RuntimeException('Profile has data after its summary'); }
            if (str_starts_with($line, 'summary:')) {
                if (!str_ends_with($raw, "\n")
                    || !preg_match('/^summary: [0-9]{1,18} -?[0-9]{1,18}$/D', $line)) {
                    throw new RuntimeException('Profile summary is incomplete');
                }
                $summary = true;
            } elseif (str_starts_with($line, 'events:')) {
                $events = preg_split('/\s+/', trim(substr($line, 7)));
                if ($events !== ['Time_(10ns)', 'Memory_(bytes)']) {
                    throw new RuntimeException('Unsupported profile event units');
                }
            } elseif (str_starts_with($line, 'fl=')) {
                $currentFile = $resolve(substr($line, 3), $files); $calleeFile = $currentFile;
            } elseif (str_starts_with($line, 'fn=')) {
                $current = $entry($currentFile, $resolve(substr($line, 3), $names)); $edge = false;
            } elseif (str_starts_with($line, 'cfl=')) {
                $calleeFile = $resolve(substr($line, 4), $files);
            } elseif (str_starts_with($line, 'cfn=')) {
                $callee = $entry($calleeFile, $resolve(substr($line, 4), $names));
            } elseif (preg_match('/^calls=([0-9]{1,18}) /D', $line, $match)) {
                if ($callee === null) { throw new RuntimeException('Profile call target unavailable'); }
                $rows[$callee]['calls'] = $add($rows[$callee]['calls'], (int)$match[1]); $edge = true;
            } elseif (preg_match('/^(?:[0-9]+|\*|[+-][0-9]+) ([0-9]{1,18}) (-?[0-9]{1,18})$/D', $line, $match)) {
                if ($current === null || $events === null) {
                    throw new RuntimeException('Profile cost lacks identity');
                }
                if (!$edge) {
                    $rows[$current]['self_time'] = $add($rows[$current]['self_time'], (int)$match[1]);
                    $rows[$current]['self_memory'] = $add($rows[$current]['self_memory'], (int)$match[2]);
                }
                $edge = false;
            }
        }
    } finally { fclose($stream); }
    if ($events === null || !$summary) { throw new RuntimeException('Profile units or final summary unavailable'); }
    $rows = array_values($rows);
    usort($rows, static fn(array $a, array $b): int =>
        ($b['self_time'] <=> $a['self_time']) ?: strcmp($a['function'] . $a['file'], $b['function'] . $b['file']));
    return ['events' => $events, 'rows' => array_slice($rows, 0, $limit), 'rows_truncated' => count($rows) > $limit];
}
"""


_PHP_LAUNCHER = r"""
$request = json_decode($argv[1], true, 16, JSON_THROW_ON_ERROR);
$check = SOURCE_GUARD;
$guardCode = SOURCE_GUARD_JSON;
$process = null; $pipes = []; $directory = null; $knownExit = null; $code = 78; $stopped = true;
$reason = null; $metrics = null; $callgraph = null;
$environment = getenv();
foreach (['PHPRC', 'PHP_INI_SCAN_DIR', 'XDEBUG_SESSION', 'XDEBUG_TRIGGER', 'PHP_IDE_CONFIG'] as $key) {
    unset($environment[$key]);
}
$environment['XDEBUG_MODE'] = 'off'; $environment['XDEBUG_CONFIG'] = '';
$options = ['-d', 'auto_prepend_file=', '-d', 'auto_append_file=', '-d', 'xdebug.mode=off',
    '-d', 'xdebug.start_with_request=no', '-d', 'xdebug.discover_client_host=0', '-d', 'xdebug.log='];
try {
    $check($request['script'], $request['source_sha256']);
    $xdebug = $request['recipe'] === 'forge-php-xdebug-v1';
    if ($xdebug) {
        $probe = proc_open([PHP_BINARY, ...$options, '-r',
            'exit(extension_loaded("xdebug") && function_exists("posix_setrlimit")'
                . ' && defined("POSIX_RLIMIT_FSIZE") ? 0 : 78);'],
            [0 => ['file', '/dev/null', 'r'], 1 => ['file', '/dev/null', 'w'], 2 => ['file', '/dev/null', 'w']],
            $unused, null, $environment);
        if (!is_resource($probe) || proc_close($probe) !== 0) {
            throw new RuntimeException('Xdebug profiling requires preinstalled Xdebug and POSIX file limits');
        }
    }
    $directory = rtrim(sys_get_temp_dir(), '/') . '/forge-php-profile-' . bin2hex(random_bytes(16));
    if (!mkdir($directory, 0700)) { throw new RuntimeException('Private profile directory unavailable'); }
    $reportFile = $directory . '/metrics.json'; $profile = $directory . '/cachegrind.out';
    $hook = '<?php (static function(): void {';
    if ($xdebug) {
        $hook .= 'if (!posix_setrlimit(POSIX_RLIMIT_FSIZE,8388608,8388608)) { exit(78); }';
    }
    $hook .= '(' . $guardCode . ')(' . var_export($request['script'], true) . ','
        . var_export($request['source_sha256'], true) . ');';
    $hook .= '$start=hrtime(true); $usage=getrusage(); $file=' . var_export($reportFile, true) . ';'
        . 'register_shutdown_function(static function() use ($start,$usage,$file): void { try {'
        . '$end=getrusage(); $us=static fn($r,$t)=>(int)$r["ru_".$t."time.tv_sec"]*1000000'
        . '+(int)$r["ru_".$t."time.tv_usec"];'
        . '$data=["wall_ns"=>hrtime(true)-$start,"cpu_user_us"=>$us($end,"u")-$us($usage,"u"),'
        . '"cpu_system_us"=>$us($end,"s")-$us($usage,"s"),"peak_memory_bytes"=>memory_get_peak_usage(true)];'
        . '@file_put_contents($file,json_encode($data,JSON_THROW_ON_ERROR),LOCK_EX); @chmod($file,0600);'
        . '} catch (Throwable $error) {}});})();';
    $hookFile = $directory . '/prepend.php';
    if (file_put_contents($hookFile, $hook, LOCK_EX) === false || !chmod($hookFile, 0600)) {
        throw new RuntimeException('Private profile collector unavailable');
    }
    $command = [PHP_BINARY, ...$options, '-d', 'register_argc_argv=1', '-d', 'auto_prepend_file=' . $hookFile];
    if ($xdebug) {
        $environment['XDEBUG_MODE'] = 'profile';
        array_push($command, '-d', 'xdebug.mode=profile', '-d', 'xdebug.start_with_request=yes',
            '-d', 'xdebug.output_dir=' . $directory, '-d', 'xdebug.profiler_output_name=cachegrind.out',
            '-d', 'xdebug.profiler_append=0', '-d', 'xdebug.use_compression=0');
    }
    array_push($command, '-f', $request['script'], '--', ...$request['args']);
    $started = hrtime(true); $deadline = $started + $request['timeout'] * 1000000000; $bytes = 0;
    $process = proc_open($command, [0 => ['file', '/dev/null', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']],
        $pipes, null, $environment);
    if (!is_resource($process)) { throw new RuntimeException('PHP child process unavailable'); }
    $stopped = false;
    foreach ($pipes as $pipe) { stream_set_blocking($pipe, false); }
    while (true) {
        $status = proc_get_status($process);
        if (!$status['running']) {
            $stopped = true;
            if ($status['signaled'] && $reason === null) { $reason = 'child_signal'; }
            if ($knownExit === null && $status['exitcode'] >= 0 && $reason === null) {
                $knownExit = $status['exitcode'];
            }
        }
        if (hrtime(true) >= $deadline && $reason === null) { $reason = 'deadline'; }
        foreach ($pipes as $index => $pipe) {
            $chunk = fread($pipe, 8192);
            if ($chunk !== false && $chunk !== '') {
                $bytes += strlen($chunk);
                if ($bytes > 1000000 && $reason === null) { $reason = 'output_bound'; }
                if ($bytes <= 1000000) { @fwrite($index === 1 ? STDOUT : STDERR, $chunk); }
            }
        }
        if ($reason !== null && !$stopped) { proc_terminate($process, 9); }
        if ($stopped && (feof($pipes[1]) && feof($pipes[2]) || $reason !== null)) { break; }
        usleep(1000);
    }
    foreach ($pipes as $pipe) { fclose($pipe); } $pipes = [];
    $closed = proc_close($process); $process = null;
    $finished = hrtime(true);
    if ($knownExit === null && $closed >= 0 && $reason === null) { $knownExit = $closed; }
    $code = $knownExit ?? 124;
    try {
        $info = lstat($reportFile);
        if ($info === false || ($info['mode'] & 0170000) !== 0100000 || $info['nlink'] !== 1 || $info['size'] > 4096) {
            throw new RuntimeException('Collector report unavailable');
        }
        $metrics = json_decode(file_get_contents($reportFile), true, 8, JSON_THROW_ON_ERROR);
        $metrics['process_wall_ns'] = $finished - $started;
        if ($xdebug) { $callgraph = forge_cachegrind($profile, $request['rows']); }
    } catch (Throwable $error) { $metrics = null; $reason ??= 'collector_unavailable'; }
    echo "\nFORGE_PHP_PROFILE_V1\n", json_encode([
        'recipe' => $request['recipe'], 'observation_only' => true, 'best_effort' => true,
        'complete' => $reason === null && $knownExit !== null, 'incomplete_reason' => $reason,
        'workload_exit_code' => $knownExit, 'metrics' => $metrics, 'callgraph' => $callgraph,
    ], JSON_THROW_ON_ERROR), "\n";
} catch (Throwable $error) {
    fwrite(STDERR, "PHP profiling prerequisite/source refusal: " . $error->getMessage() . "\n");
    $code = $knownExit ?? 78;
} finally {
    if (is_resource($process)) {
        proc_terminate($process, 9);
        $drainDeadline = hrtime(true) + 1000000000;
        do { $status = proc_get_status($process); if (!$status['running']) { $stopped = true; break; } usleep(1000); }
        while (hrtime(true) < $drainDeadline);
        foreach ($pipes as $pipe) { fclose($pipe); }
        if ($stopped) { proc_close($process); }
    }
    if ($directory !== null && $stopped) {
        foreach (['prepend.php', 'metrics.json', 'cachegrind.out'] as $name) { @unlink($directory . '/' . $name); }
        @rmdir($directory);
    }
}
exit($code);
"""


def build_php_profile_argv(request: PHPProfileRequest, source_sha256: str) -> list[str]:
    request = PHPProfileRequest.model_validate(request.model_dump(mode="json"))
    if re.fullmatch(r"[a-f0-9]{64}", source_sha256) is None:
        raise ValueError("PHP profiling requires the observed source SHA256")
    payload = {**request.model_dump(mode="json"), "source_sha256": source_sha256}
    guard_literal = "'" + _SOURCE_GUARD.replace("\\", "\\\\").replace("'", "\\'") + "'"
    launcher = _PHP_LAUNCHER.replace("SOURCE_GUARD_JSON", guard_literal).replace("SOURCE_GUARD", _SOURCE_GUARD)
    code = _CACHEGRIND_PARSER + launcher
    # Only the trusted recipe uses this private file; the application retains its real entrypoint.
    bootstrap = r"""
$payload = $argv[1]; $code = '<?php ' . implode('', array_slice($argv, 2));
if (strlen($code) > 65536) { exit(78); }
$file = tempnam(sys_get_temp_dir(), 'forge-php-launcher-');
if ($file === false || !chmod($file, 0600)) { exit(78); }
register_shutdown_function(static function() use ($file): void { @unlink($file); });
if (file_put_contents($file, $code, LOCK_EX) === false) { exit(78); }
$argv = ['forge-php-profile', $payload];
require $file;
"""
    return [
        "php",
        "-n",
        "-d",
        "memory_limit=64M",
        "-r",
        bootstrap,
        "--",
        json.dumps(payload, separators=(",", ":")),
        *(code[index : index + 4000] for index in range(0, len(code), 4000)),
    ]
