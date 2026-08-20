from __future__ import annotations

from pathlib import Path
from typing import Any

from .api import QaClient
from .checkout import Checkout, repository_diff
from .config import load_source_files
from .execution import CommandExecutor, execute_commands
from .models import CommandResult, ProjectPlan, WorkflowResult


class FixApplicationError(RuntimeError):
    pass


class RepoQaWorkflow:
    def __init__(self, executor: CommandExecutor, qa_client: QaClient) -> None:
        self.executor = executor
        self.qa_client = qa_client

    def run(
        self,
        checkout: Checkout,
        plan: ProjectPlan,
        *,
        fix_mode: str = "recommend",
        pipeline_id: str = "",
    ) -> WorkflowResult:
        if fix_mode not in {"recommend", "propose", "apply"}:
            raise ValueError("fix_mode must be recommend, propose, or apply")
        command_results = execute_commands(self.executor, plan.commands, checkout.root)
        initial_payload = _evaluation_payload(
            checkout,
            plan,
            command_results,
            mode="propose_fix" if fix_mode in {"propose", "apply"} else "recommend",
            pipeline_id=pipeline_id,
        )
        initial = self.qa_client.submit(initial_payload)
        applied: list[dict[str, Any]] = []
        verification_commands: list[CommandResult] = []
        verification: dict[str, Any] | None = None
        if fix_mode == "apply" and initial.get("proposed_changes"):
            applied = apply_proposed_changes(checkout.root, initial["proposed_changes"])
            verify_specs = [command for command in plan.commands if command.phase not in {"setup", "install"}]
            verification_commands = execute_commands(self.executor, verify_specs, checkout.root)
            refreshed_sources = load_source_files(checkout.root, [source["path"] for source in plan.source_files])
            verification_plan = ProjectPlan(
                root=plan.root,
                config=plan.config,
                stacks=plan.stacks,
                commands=verify_specs,
                documentation=plan.documentation,
                source_files=refreshed_sources,
            )
            verification = self.qa_client.submit(
                _evaluation_payload(
                    checkout,
                    verification_plan,
                    verification_commands,
                    mode="recommend",
                    pipeline_id=f"{pipeline_id}:verification" if pipeline_id else "verification",
                )
            )
        final_evaluation = verification or initial
        return WorkflowResult(
            project_id=plan.config.project_id,
            stacks=plan.stacks,
            revision=checkout.revision,
            branch=checkout.branch,
            initial_evaluation=initial,
            verification_evaluation=verification,
            applied_changes=applied,
            commands=command_results,
            verification_commands=verification_commands,
            diff=repository_diff(checkout.root),
            passed=bool((final_evaluation.get("quality_gate") or {}).get("passed")),
            workspace=str(checkout.root),
        )


def apply_proposed_changes(root: Path, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    root = root.resolve()
    pending: dict[Path, str] = {}
    normalized: list[dict[str, Any]] = []
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal, dict):
            raise FixApplicationError(f"Proposal {index} is not an object")
        relative = str(proposal.get("path") or "")
        original = proposal.get("original")
        replacement = proposal.get("replacement")
        documentation_path = str(proposal.get("documentation_path") or "")
        if not relative or not isinstance(original, str) or not original or not isinstance(replacement, str) or not documentation_path:
            raise FixApplicationError(f"Proposal {index} is missing path, exact source text, replacement, or documentation")
        target = (root / relative).resolve()
        if target == root or root not in target.parents or not target.is_file():
            raise FixApplicationError(f"Proposal target is outside the checkout or missing: {relative}")
        content = pending.get(target)
        if content is None:
            content = target.read_text(encoding="utf-8")
        occurrences = content.count(original)
        if occurrences != 1:
            raise FixApplicationError(f"Proposal for {relative} expected one exact match; found {occurrences}")
        pending[target] = content.replace(original, replacement, 1)
        normalized.append(
            {
                "rule_id": str(proposal.get("rule_id") or ""),
                "finding_id": str(proposal.get("finding_id") or ""),
                "path": target.relative_to(root).as_posix(),
                "documentation_path": documentation_path,
                "rationale": str(proposal.get("rationale") or ""),
            }
        )
    for target, content in pending.items():
        target.write_text(content, encoding="utf-8")
    return normalized


def _evaluation_payload(
    checkout: Checkout,
    plan: ProjectPlan,
    results: list[CommandResult],
    *,
    mode: str,
    pipeline_id: str,
) -> dict[str, Any]:
    checks = [result.qa_check() for result in results if result.kind != "runtime"]
    runtime = [result.runtime_observation() for result in results if result.kind == "runtime"]
    return {
        "project": {
            "project_id": plan.config.project_id,
            "name": plan.config.project_name,
            "repository": checkout.source[:500],
            "team": plan.config.team,
        },
        "revision": checkout.revision,
        "branch": checkout.branch,
        "mode": mode,
        "pipeline": {"provider": "cascade-repo-qa-runner", "pipeline_id": pipeline_id[:200]},
        "checks": checks,
        "runtime_observations": runtime,
        "documentation": plan.documentation,
        "documented_fixes": plan.config.documented_fixes,
        "source_files": plan.source_files,
        "fail_on": plan.config.fail_on,
    }
