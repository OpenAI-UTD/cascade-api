from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from services.shared.qa.schemas import DocumentedFixRule

from .models import CommandSpec, ProjectPlan, RunnerConfig


DEFAULT_DOC_PATTERNS = ["README*", "CONTRIBUTING*", "docs/**/*.md", ".cascade/**/*.md"]
STACK_IMAGES = {
    "python": "python:3.12-slim",
    "node": "node:22-slim",
    "go": "golang:1.23",
    "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
    "maven": "maven:3.9-eclipse-temurin-21",
    "gradle": "gradle:8.12-jdk21",
}
STACK_EXECUTABLES = {
    "python": {"python", ".cascade-venv/bin/python"},
    "node": {"node", "npm"},
    "go": {"go"},
    "dotnet": {"dotnet"},
    "maven": {"mvn"},
    "gradle": {"gradle", "./gradlew"},
}
VALID_KINDS = {"test", "lint", "build", "security", "runtime", "other"}
VALID_PHASES = {"setup", "install", "build", "test", "lint", "security", "runtime"}


class RunnerConfigError(ValueError):
    pass


def load_runner_config(root: Path, config_path: Path | None = None, *, allow_project_commands: bool = False) -> RunnerConfig:
    path = config_path or root / ".cascade" / "qa-runner.json"
    raw: dict[str, Any] = {}
    if path.exists():
        resolved = _contained(root, path)
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerConfigError(f"Invalid runner configuration {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RunnerConfigError("Runner configuration must be a JSON object")

    inferred_name = root.name or "project"
    project_id = _project_id(str(raw.get("project_id") or inferred_name))
    project_name = str(raw.get("project_name") or inferred_name).strip()[:200]
    requested_stacks = raw.get("stacks") or ([] if raw.get("stack") in {None, "auto"} else [raw.get("stack")])
    if not isinstance(requested_stacks, list):
        raise RunnerConfigError("stacks must be a JSON array")
    stacks = [_validate_stack(str(item)) for item in requested_stacks]
    docs = raw.get("documentation") or DEFAULT_DOC_PATTERNS
    if not isinstance(docs, list) or not all(isinstance(item, str) and item.strip() for item in docs):
        raise RunnerConfigError("documentation must be an array of non-empty glob patterns")
    fixes_raw = raw.get("documented_fixes") or []
    if not isinstance(fixes_raw, list) or not all(isinstance(item, dict) for item in fixes_raw):
        raise RunnerConfigError("documented_fixes must be an array of objects")
    try:
        fixes = [DocumentedFixRule.model_validate(item).model_dump(mode="json") for item in fixes_raw]
    except ValidationError as exc:
        raise RunnerConfigError(f"Invalid documented fix rule: {exc}") from exc
    source_files = raw.get("source_files") or []
    if not isinstance(source_files, list) or not all(isinstance(item, str) and item.strip() for item in source_files):
        raise RunnerConfigError("source_files must be an array of paths")
    fail_on = str(raw.get("fail_on") or "high")
    if fail_on not in {"info", "low", "medium", "high", "critical"}:
        raise RunnerConfigError("fail_on must be info, low, medium, high, or critical")
    commands = _custom_commands(raw.get("commands") or [], stacks, allow_project_commands)
    return RunnerConfig(
        project_id=project_id,
        project_name=project_name,
        team=str(raw.get("team") or "")[:200],
        stacks=stacks,
        documentation_patterns=[str(item) for item in docs],
        documented_fixes=fixes,
        source_files=[str(item) for item in source_files],
        custom_commands=commands,
        replace_detected_commands=bool(raw.get("replace_detected_commands", False)),
        fail_on=fail_on,
    )


def plan_project(root: Path, config: RunnerConfig) -> ProjectPlan:
    root = root.resolve()
    stacks = config.stacks or detect_stacks(root)
    if not stacks:
        raise RunnerConfigError("Could not detect a supported stack; configure stacks and approved commands in .cascade/qa-runner.json")
    detected = [] if config.replace_detected_commands else default_commands(root, stacks)
    commands = detected + config.custom_commands
    if not commands:
        raise RunnerConfigError("No QA commands were detected or configured")
    documentation = load_documentation(root, config.documentation_patterns)
    source_paths = list(config.source_files)
    for rule in config.documented_fixes:
        for change in rule.get("changes", []) if isinstance(rule.get("changes"), list) else []:
            if isinstance(change, dict) and isinstance(change.get("path"), str):
                source_paths.append(change["path"])
    return ProjectPlan(
        root=root,
        config=config,
        stacks=stacks,
        commands=commands,
        documentation=documentation,
        source_files=load_source_files(root, source_paths),
    )


def detect_stacks(root: Path) -> list[str]:
    stacks: list[str] = []
    if any((root / marker).exists() for marker in ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"]):
        stacks.append("python")
    if (root / "package.json").exists():
        stacks.append("node")
    if (root / "go.mod").exists():
        stacks.append("go")
    if list(root.glob("*.sln")) or list(root.glob("*.csproj")):
        stacks.append("dotnet")
    if (root / "pom.xml").exists():
        stacks.append("maven")
    if any((root / marker).exists() for marker in ["build.gradle", "build.gradle.kts", "gradlew"]):
        stacks.append("gradle")
    if not stacks and any(root.glob("*.py")):
        stacks.append("python")
    return stacks


def default_commands(root: Path, stacks: list[str]) -> list[CommandSpec]:
    commands: list[CommandSpec] = []
    for stack in stacks:
        if stack == "python":
            commands.extend(_python_commands(root))
        elif stack == "node":
            commands.extend(_node_commands(root))
        elif stack == "go":
            env = {"GOMODCACHE": "/workspace/.cascade-cache/go/pkg/mod", "GOCACHE": "/workspace/.cascade-cache/go/build"}
            commands.extend(
                [
                    _cmd("Go dependency download", "build", "install", ["go", "mod", "download"], stack, network=True, env=env),
                    _cmd("Go vet", "lint", "lint", ["go", "vet", "./..."], stack, env=env),
                    _cmd("Go tests", "test", "test", ["go", "test", "./..."], stack, env=env),
                ]
            )
        elif stack == "dotnet":
            env = {"NUGET_PACKAGES": "/workspace/.cascade-cache/nuget"}
            commands.extend(
                [
                    _cmd(".NET restore", "build", "install", ["dotnet", "restore"], stack, network=True, env=env),
                    _cmd(".NET build", "build", "build", ["dotnet", "build", "--no-restore"], stack, env=env),
                    _cmd(".NET tests", "test", "test", ["dotnet", "test", "--no-restore"], stack, env=env),
                ]
            )
        elif stack == "maven":
            commands.extend(
                [
                    _cmd(
                        "Maven dependency preparation",
                        "build",
                        "install",
                        ["mvn", "-B", "-Dmaven.repo.local=/workspace/.cascade-cache/m2", "dependency:go-offline"],
                        stack,
                        network=True,
                        timeout=900,
                    ),
                    _cmd(
                        "Maven tests",
                        "test",
                        "test",
                        ["mvn", "-B", "-o", "-Dmaven.repo.local=/workspace/.cascade-cache/m2", "test"],
                        stack,
                        timeout=900,
                    ),
                ]
            )
        elif stack == "gradle":
            executable = "./gradlew" if (root / "gradlew").exists() else "gradle"
            env = {"GRADLE_USER_HOME": "/workspace/.cascade-cache/gradle"}
            commands.extend(
                [
                    _cmd("Gradle dependency preparation", "build", "install", [executable, "--no-daemon", "dependencies"], stack, network=True, timeout=900, env=env),
                    _cmd("Gradle tests", "test", "test", [executable, "--no-daemon", "--offline", "test"], stack, timeout=900, env=env),
                ]
            )
    return commands


def load_documentation(root: Path, patterns: list[str]) -> list[dict[str, Any]]:
    seen: set[Path] = set()
    docs: list[dict[str, Any]] = []
    for pattern in patterns:
        for candidate in sorted(root.glob(pattern)):
            if not candidate.is_file():
                continue
            resolved = _contained(root, candidate)
            if resolved in seen or resolved.stat().st_size > 100_000:
                continue
            seen.add(resolved)
            content = resolved.read_text(encoding="utf-8", errors="replace")
            if content.strip():
                docs.append({"path": resolved.relative_to(root).as_posix(), "content": content, "tags": ["project-documentation"]})
            if len(docs) >= 100:
                return docs
    return docs


def load_source_files(root: Path, paths: list[str]) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for value in dict.fromkeys(paths):
        resolved = _contained(root, root / value)
        if not resolved.is_file():
            raise RunnerConfigError(f"Configured source file does not exist: {value}")
        if resolved.stat().st_size > 200_000:
            raise RunnerConfigError(f"Configured source file exceeds 200 KB: {value}")
        files.append({"path": resolved.relative_to(root).as_posix(), "content": resolved.read_text(encoding="utf-8", errors="replace")})
    return files


def _python_commands(root: Path) -> list[CommandSpec]:
    commands: list[CommandSpec] = []
    requirements = [name for name in ["requirements.txt", "requirements-dev.txt"] if (root / name).exists()]
    python = "python"
    install_project = not requirements and any((root / marker).exists() for marker in ["pyproject.toml", "setup.py", "setup.cfg"])
    if requirements or install_project:
        commands.append(_cmd("Create Python QA environment", "build", "setup", ["python", "-m", "venv", ".cascade-venv"], "python"))
        python = ".cascade-venv/bin/python"
        for requirement in requirements:
            commands.append(_cmd(f"Install {requirement}", "build", "install", [python, "-m", "pip", "install", "-r", requirement], "python", network=True, timeout=900))
        if install_project:
            commands.append(_cmd("Install Python project", "build", "install", [python, "-m", "pip", "install", "-e", "."], "python", network=True, timeout=900))
    commands.append(_cmd("Python compile", "build", "build", [python, "-m", "compileall", "-q", "."], "python"))
    has_tests = (root / "tests").exists() or any(root.glob("test_*.py"))
    if has_tests:
        pytest_project = any((root / marker).exists() for marker in ["pytest.ini", "conftest.py"]) or _contains(root / "pyproject.toml", "pytest") or _tree_contains(root / "tests", "import pytest")
        argv = [python, "-m", "pytest", "-q"] if pytest_project else [python, "-m", "unittest", "discover", "-v"]
        commands.append(_cmd("Python tests", "test", "test", argv, "python", timeout=900))
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists() or _contains(root / "pyproject.toml", "[tool.ruff"):
        commands.append(_cmd("Ruff", "lint", "lint", [python, "-m", "ruff", "check", "."], "python"))
    return commands


def _node_commands(root: Path) -> list[CommandSpec]:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    scripts = package.get("scripts") if isinstance(package, dict) else {}
    scripts = scripts if isinstance(scripts, dict) else {}
    install = ["npm", "ci", "--ignore-scripts"] if (root / "package-lock.json").exists() else ["npm", "install", "--ignore-scripts"]
    commands = [_cmd("Node dependency install", "build", "install", install, "node", network=True, timeout=900)]
    for script, kind, phase in [("lint", "lint", "lint"), ("test", "test", "test"), ("build", "build", "build"), ("qa:runtime", "runtime", "runtime"), ("smoke", "runtime", "runtime")]:
        if script in scripts:
            commands.append(_cmd(f"npm run {script}", kind, phase, ["npm", "run", script], "node", timeout=900 if phase in {"test", "build"} else 300))
    return commands


def _custom_commands(raw_commands: Any, configured_stacks: list[str], allowed: bool) -> list[CommandSpec]:
    if not isinstance(raw_commands, list):
        raise RunnerConfigError("commands must be a JSON array")
    if raw_commands and not allowed:
        raise RunnerConfigError("Project-defined commands require --allow-project-commands")
    commands: list[CommandSpec] = []
    for index, raw in enumerate(raw_commands):
        if not isinstance(raw, dict):
            raise RunnerConfigError(f"commands[{index}] must be an object")
        stack = _validate_stack(str(raw.get("stack") or (configured_stacks[0] if len(configured_stacks) == 1 else "")))
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value and "\x00" not in value for value in argv):
            raise RunnerConfigError(f"commands[{index}].argv must be a non-empty string array")
        if argv[0] not in STACK_EXECUTABLES[stack]:
            raise RunnerConfigError(f"commands[{index}] executable is not approved for {stack}: {argv[0]}")
        kind = str(raw.get("kind") or "other")
        phase = str(raw.get("phase") or ("runtime" if kind == "runtime" else kind))
        if kind not in VALID_KINDS or phase not in VALID_PHASES:
            raise RunnerConfigError(f"commands[{index}] has an unsupported kind or phase")
        timeout = int(raw.get("timeout_seconds") or 300)
        if timeout < 1 or timeout > 1800:
            raise RunnerConfigError(f"commands[{index}].timeout_seconds must be between 1 and 1800")
        environment = raw.get("environment") or {}
        if not isinstance(environment, dict) or not all(_safe_env(str(key), str(value)) for key, value in environment.items()):
            raise RunnerConfigError(f"commands[{index}].environment contains an invalid name or value")
        requires_network = bool(raw.get("requires_network", False))
        if requires_network and phase != "install":
            raise RunnerConfigError(f"commands[{index}] may request network only during the install phase")
        commands.append(
            CommandSpec(
                name=str(raw.get("name") or " ".join(argv))[:200],
                kind=kind,  # type: ignore[arg-type]
                phase=phase,  # type: ignore[arg-type]
                argv=tuple(argv),
                image=STACK_IMAGES[stack],
                timeout_seconds=timeout,
                requires_network=requires_network,
                origin="project",
                environment={str(key): str(value) for key, value in environment.items()},
            )
        )
    return commands


def _cmd(
    name: str,
    kind: str,
    phase: str,
    argv: list[str],
    stack: str,
    *,
    network: bool = False,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> CommandSpec:
    return CommandSpec(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        argv=tuple(argv),
        image=STACK_IMAGES[stack],
        timeout_seconds=timeout,
        requires_network=network,
        environment=env or {},
    )


def _contained(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise RunnerConfigError(f"Path escapes project root: {candidate}")
    return resolved


def _project_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")[:120]
    if not normalized:
        raise RunnerConfigError("project_id must contain letters or numbers")
    return normalized


def _validate_stack(value: str) -> str:
    if value not in STACK_IMAGES:
        raise RunnerConfigError(f"Unsupported stack: {value or '<missing>'}")
    return value


def _contains(path: Path, needle: str) -> bool:
    try:
        return needle.lower() in path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _tree_contains(root: Path, needle: str) -> bool:
    if not root.exists():
        return False
    return any(_contains(path, needle) for path in list(root.rglob("*.py"))[:200])


def _safe_env(key: str, value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z_][A-Z0-9_]{0,63}", key)) and "\x00" not in value and len(value) <= 2000
