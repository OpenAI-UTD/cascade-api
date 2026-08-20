from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from services.shared.repo_qa.api import InProcessQaClient
from services.shared.repo_qa.checkout import CheckoutError, checkout_repository, cleanup_checkout
from services.shared.repo_qa.config import RunnerConfigError, detect_stacks, load_runner_config, plan_project
from services.shared.repo_qa.execution import DockerExecutor, LocalExecutor
from services.shared.repo_qa.models import CommandSpec
from services.shared.repo_qa.workflow import FixApplicationError, RepoQaWorkflow, apply_proposed_changes


def test_detects_multiple_supported_stacks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node test.js"}}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.test/demo\n", encoding="utf-8")

    assert detect_stacks(tmp_path) == ["python", "node", "go"]


def test_project_commands_require_explicit_approval(tmp_path: Path) -> None:
    config_dir = tmp_path / ".cascade"
    config_dir.mkdir()
    (config_dir / "qa-runner.json").write_text(
        json.dumps(
            {
                "stacks": ["python"],
                "commands": [{"name": "tests", "stack": "python", "kind": "test", "phase": "test", "argv": ["python", "-m", "unittest"]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RunnerConfigError, match="--allow-project-commands"):
        load_runner_config(tmp_path)

    config = load_runner_config(tmp_path, allow_project_commands=True)
    assert config.custom_commands[0].origin == "project"


def test_project_command_executable_is_stack_allowlisted(tmp_path: Path) -> None:
    config_dir = tmp_path / ".cascade"
    config_dir.mkdir()
    (config_dir / "qa-runner.json").write_text(
        json.dumps(
            {
                "stacks": ["python"],
                "commands": [{"name": "unsafe", "stack": "python", "kind": "test", "argv": ["bash", "-c", "echo unsafe"]}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RunnerConfigError, match="not approved"):
        load_runner_config(tmp_path, allow_project_commands=True)


def test_docker_executor_builds_hardened_offline_command(tmp_path: Path) -> None:
    spec = CommandSpec(name="tests", kind="test", phase="test", argv=("python", "-m", "unittest"), image="python:3.12-slim")
    command = DockerExecutor().docker_command(spec, tmp_path)

    assert command[:3] == ["docker", "run", "--rm"]
    assert "--pull=never" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[-4:] == ["python:3.12-slim", "python", "-m", "unittest"]


def test_dependency_network_requires_explicit_approval(tmp_path: Path) -> None:
    called = False

    def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("process should not run")

    spec = CommandSpec(
        name="install",
        kind="build",
        phase="install",
        argv=("python", "-m", "pip", "install", "-r", "requirements.txt"),
        image="python:3.12-slim",
        requires_network=True,
    )
    result = DockerExecutor(process_runner=should_not_run).run(spec, tmp_path)

    assert result.status == "skipped"
    assert "not approved" in result.output
    assert called is False


def test_fix_application_is_atomic_and_path_bounded(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    proposals = [
        {"path": "first.py", "original": "VALUE = 1", "replacement": "VALUE = 2", "documentation_path": "docs/fix.md"},
        {"path": "missing.py", "original": "x", "replacement": "y", "documentation_path": "docs/fix.md"},
    ]

    with pytest.raises(FixApplicationError, match="outside the checkout or missing"):
        apply_proposed_changes(tmp_path, proposals)

    assert first.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_checkout_rejects_remote_without_approval() -> None:
    with pytest.raises(CheckoutError, match="--allow-remote"):
        checkout_repository("https://example.invalid/team/project.git")


def test_full_runner_fixes_and_retests_disposable_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source-project"
    source.mkdir()
    (source / "docs").mkdir()
    (source / ".cascade").mkdir()
    (source / "app_config.py").write_text("RETRY_TIMEOUT_SECONDS = 10\n", encoding="utf-8")
    (source / "test_config.py").write_text(
        "import unittest\n"
        "from app_config import RETRY_TIMEOUT_SECONDS\n\n"
        "class ConfigTest(unittest.TestCase):\n"
        "    def test_retry_timeout(self):\n"
        "        self.assertEqual(RETRY_TIMEOUT_SECONDS, 30, 'timeout must be 30 seconds')\n",
        encoding="utf-8",
    )
    (source / "runtime_smoke.py").write_text(
        "from app_config import RETRY_TIMEOUT_SECONDS\n"
        "if RETRY_TIMEOUT_SECONDS != 30:\n"
        "    raise SystemExit('runtime smoke failed: timeout must be 30 seconds')\n"
        "print('runtime smoke passed')\n",
        encoding="utf-8",
    )
    (source / "README.md").write_text("# Demo project\n", encoding="utf-8")
    (source / "docs" / "retries.md").write_text("Retry timeout must be 30 seconds.\n", encoding="utf-8")
    (source / ".cascade" / "qa-runner.json").write_text(
        json.dumps(
            {
                "project_id": "runner-fixture",
                    "project_name": "Runner Fixture",
                    "fail_on": "high",
                    "stacks": ["python"],
                    "replace_detected_commands": True,
                    "commands": [
                        {"name": "Python compile", "stack": "python", "kind": "build", "phase": "build", "argv": ["python", "-m", "compileall", "-q", "."]},
                        {"name": "Python tests", "stack": "python", "kind": "test", "phase": "test", "argv": ["python", "-m", "unittest", "discover", "-v"]},
                        {"name": "Runtime smoke", "stack": "python", "kind": "runtime", "phase": "runtime", "argv": ["python", "runtime_smoke.py"]},
                    ],
                "documented_fixes": [
                    {
                        "rule_id": "retry-timeout",
                        "issue_patterns": ["timeout must be 30 seconds"],
                        "title": "Use documented timeout",
                        "recommendation": "Set the timeout to 30 according to docs/retries.md.",
                        "documentation_path": "docs/retries.md",
                        "changes": [{"path": "app_config.py", "original": "RETRY_TIMEOUT_SECONDS = 10", "replacement": "RETRY_TIMEOUT_SECONDS = 30"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _git(source, "init")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Cascade Test", "-c", "user.email=cascade@example.invalid", "commit", "-m", "fixture")

    checkout = checkout_repository(str(source), workspace_root=tmp_path)
    try:
        config = load_runner_config(checkout.root, allow_project_commands=True)
        plan = plan_project(checkout.root, config)
        assert plan.stacks == ["python"]
        assert any(command.name == "Python tests" for command in plan.commands)

        outcome = RepoQaWorkflow(LocalExecutor(), InProcessQaClient()).run(checkout, plan, fix_mode="apply", pipeline_id="test-run")

        assert outcome.initial_evaluation["status"] == "fixes_proposed"
        assert outcome.initial_evaluation["quality_gate"]["passed"] is False
        assert outcome.initial_evaluation["stats"]["runtime_observations"] == 1
        assert len(outcome.applied_changes) == 1
        assert outcome.verification_evaluation is not None
        assert outcome.verification_evaluation["status"] == "passed"
        assert outcome.verification_evaluation["stats"]["runtime_observations"] == 1
        assert outcome.passed is True
        assert "RETRY_TIMEOUT_SECONDS = 30" in (checkout.root / "app_config.py").read_text(encoding="utf-8")
        assert "RETRY_TIMEOUT_SECONDS = 30" in outcome.diff
    finally:
        cleanup_checkout(checkout)


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
