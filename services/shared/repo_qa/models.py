from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


CheckKind = Literal["test", "lint", "build", "security", "runtime", "other"]
CommandPhase = Literal["setup", "install", "build", "test", "lint", "security", "runtime"]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    kind: CheckKind
    phase: CommandPhase
    argv: tuple[str, ...]
    image: str
    timeout_seconds: int = 300
    requires_network: bool = False
    origin: Literal["detected", "project"] = "detected"
    environment: dict[str, str] = field(default_factory=dict)


@dataclass
class RunnerConfig:
    project_id: str
    project_name: str
    team: str = ""
    stacks: list[str] = field(default_factory=list)
    documentation_patterns: list[str] = field(default_factory=list)
    documented_fixes: list[dict[str, Any]] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    custom_commands: list[CommandSpec] = field(default_factory=list)
    replace_detected_commands: bool = False
    fail_on: str = "high"


@dataclass(frozen=True)
class ProjectPlan:
    root: Path
    config: RunnerConfig
    stacks: list[str]
    commands: list[CommandSpec]
    documentation: list[dict[str, Any]]
    source_files: list[dict[str, str]]


@dataclass(frozen=True)
class CommandResult:
    name: str
    kind: CheckKind
    phase: CommandPhase
    status: Literal["passed", "failed", "error", "skipped"]
    exit_code: int | None
    output: str
    duration_ms: float
    timed_out: bool = False
    argv: tuple[str, ...] = ()
    image: str = ""

    def qa_check(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "kind": self.kind if self.kind != "runtime" else "other",
            "output": self.output,
        }

    def runtime_observation(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": "healthy" if self.status == "passed" else "failed",
            "message": self.output or f"Runtime command exited with status {self.status}",
        }


@dataclass
class WorkflowResult:
    project_id: str
    stacks: list[str]
    revision: str
    branch: str
    initial_evaluation: dict[str, Any]
    verification_evaluation: dict[str, Any] | None
    applied_changes: list[dict[str, Any]]
    commands: list[CommandResult]
    verification_commands: list[CommandResult]
    diff: str
    passed: bool
    workspace: str

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["commands"] = [asdict(result) for result in self.commands]
        payload["verification_commands"] = [asdict(result) for result in self.verification_commands]
        return payload
