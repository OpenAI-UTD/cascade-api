from __future__ import annotations

import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from .models import CommandResult, CommandSpec


class CommandExecutor(ABC):
    @abstractmethod
    def run(self, spec: CommandSpec, workspace: Path) -> CommandResult:
        raise NotImplementedError


class DockerExecutor(CommandExecutor):
    def __init__(
        self,
        *,
        allow_dependency_network: bool = False,
        pull_images: bool = False,
        memory: str = "1g",
        cpus: str = "1.0",
        pids_limit: int = 256,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.allow_dependency_network = allow_dependency_network
        self.pull_images = pull_images
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.process_runner = process_runner

    def run(self, spec: CommandSpec, workspace: Path) -> CommandResult:
        if spec.requires_network and not self.allow_dependency_network:
            return CommandResult(
                name=spec.name,
                kind=spec.kind,
                phase=spec.phase,
                status="skipped",
                exit_code=None,
                output="Dependency network access was not approved. Re-run with --allow-dependency-network.",
                duration_ms=0.0,
                argv=spec.argv,
                image=spec.image,
            )
        return _run_process(spec, self.docker_command(spec, workspace), workspace, self.process_runner, environment=None)

    def docker_command(self, spec: CommandSpec, workspace: Path) -> list[str]:
        command = [
            "docker",
            "run",
            "--rm",
            f"--pull={'missing' if self.pull_images else 'never'}",
            "--network",
            "bridge" if spec.requires_network and self.allow_dependency_network else "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--workdir",
            "/workspace",
            "--mount",
            f"type=bind,source={workspace.resolve()},target=/workspace",
            "--env",
            "HOME=/tmp/home",
            "--env",
            "CI=true",
        ]
        for key, value in sorted(spec.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        if os.name != "nt" and hasattr(os, "getuid"):
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        command.extend([spec.image, *spec.argv])
        return command


class LocalExecutor(CommandExecutor):
    """Unsafe test-only executor. Project commands run directly on the host."""

    def __init__(self, *, allow_dependency_network: bool = False, process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self.allow_dependency_network = allow_dependency_network
        self.process_runner = process_runner

    def run(self, spec: CommandSpec, workspace: Path) -> CommandResult:
        if spec.requires_network and not self.allow_dependency_network:
            return CommandResult(
                name=spec.name,
                kind=spec.kind,
                phase=spec.phase,
                status="skipped",
                exit_code=None,
                output="Dependency network access was not approved.",
                duration_ms=0.0,
                argv=spec.argv,
            )
        environment = _local_environment(workspace, spec.environment)
        return _run_process(spec, list(spec.argv), workspace, self.process_runner, environment=environment)


def execute_commands(executor: CommandExecutor, commands: list[CommandSpec], workspace: Path) -> list[CommandResult]:
    return [executor.run(command, workspace) for command in commands]


def _run_process(
    spec: CommandSpec,
    command: list[str],
    workspace: Path,
    process_runner: Callable[..., subprocess.CompletedProcess[str]],
    *,
    environment: dict[str, str] | None,
) -> CommandResult:
    started = time.perf_counter()
    try:
        completed = process_runner(
            command,
            cwd=str(workspace),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=spec.timeout_seconds,
            check=False,
        )
        status = "passed" if completed.returncode == 0 else "failed"
        output = (completed.stdout or "")[-20_000:]
        return CommandResult(
            name=spec.name,
            kind=spec.kind,
            phase=spec.phase,
            status=status,
            exit_code=completed.returncode,
            output=output,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            argv=spec.argv,
            image=spec.image,
        )
    except subprocess.TimeoutExpired as exc:
        output = _timeout_output(exc)
        return CommandResult(
            name=spec.name,
            kind=spec.kind,
            phase=spec.phase,
            status="error",
            exit_code=None,
            output=f"Command timed out after {spec.timeout_seconds}s.\n{output}"[-20_000:],
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            timed_out=True,
            argv=spec.argv,
            image=spec.image,
        )
    except OSError as exc:
        return CommandResult(
            name=spec.name,
            kind=spec.kind,
            phase=spec.phase,
            status="error",
            exit_code=None,
            output=f"Command could not start: {exc.__class__.__name__}: {exc}"[-20_000:],
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            argv=spec.argv,
            image=spec.image,
        )


def _local_environment(workspace: Path, additions: dict[str, str]) -> dict[str, str]:
    keep = ["PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL"]
    environment = {key: os.environ[key] for key in keep if key in os.environ}
    environment.update({"HOME": str(workspace / ".cascade-home"), "CI": "true", **additions})
    return environment


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    output = exc.stdout or ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output
