from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from services.shared.repo_qa.api import HttpQaClient  # noqa: E402
from services.shared.repo_qa.checkout import Checkout, checkout_repository, cleanup_checkout  # noqa: E402
from services.shared.repo_qa.config import load_runner_config, plan_project  # noqa: E402
from services.shared.repo_qa.execution import DockerExecutor, LocalExecutor  # noqa: E402
from services.shared.repo_qa.workflow import RepoQaWorkflow  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Checkout a project, run isolated QA checks, submit evidence, and verify documented fixes.")
    parser.add_argument("repository", help="Local Git repository path or explicitly approved remote URL")
    parser.add_argument("--revision", default="", help="Branch, tag, or commit to check out")
    parser.add_argument("--api-url", default=os.getenv("CASCADE_QA_API_URL", "http://localhost:8040"))
    parser.add_argument("--config", default=".cascade/qa-runner.json", help="Configuration path inside the project")
    parser.add_argument("--result", default="repo-qa-result.json", help="Runner report JSON path")
    parser.add_argument("--workspace-root", default="", help="Parent directory for disposable checkouts")
    parser.add_argument("--fix-mode", choices=["recommend", "propose", "apply"], default="recommend")
    parser.add_argument("--executor", choices=["docker", "local"], default="docker")
    parser.add_argument("--allow-remote", action="store_true", help="Allow network Git repository sources")
    parser.add_argument("--allow-project-commands", action="store_true", help="Approve commands committed in .cascade/qa-runner.json")
    parser.add_argument("--allow-dependency-network", action="store_true", help="Allow network only for commands marked as dependency setup")
    parser.add_argument("--pull-images", action="store_true", help="Allow Docker to pull missing approved stack images")
    parser.add_argument("--allow-local-execution", action="store_true", help="Acknowledge that the local executor is not isolated")
    parser.add_argument("--keep-workspace", action="store_true", help="Keep the disposable checkout after the run")
    parser.add_argument("--plan-only", action="store_true", help="Print the detected plan without executing project code")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.executor == "local" and not args.allow_local_execution:
        print("ERROR: local execution is not isolated; pass --allow-local-execution only for controlled testing", file=sys.stderr)
        return 1
    checkout: Checkout | None = None
    try:
        workspace_root = Path(args.workspace_root).resolve() if args.workspace_root else None
        checkout = checkout_repository(args.repository, revision=args.revision, workspace_root=workspace_root, allow_remote=args.allow_remote)
        config_path = checkout.root / args.config
        config = load_runner_config(checkout.root, config_path, allow_project_commands=args.allow_project_commands)
        plan = plan_project(checkout.root, config)
        _print_plan(checkout, plan)
        if args.plan_only:
            return 0
        executor = (
            DockerExecutor(
                allow_dependency_network=args.allow_dependency_network,
                pull_images=args.pull_images,
            )
            if args.executor == "docker"
            else LocalExecutor(allow_dependency_network=args.allow_dependency_network)
        )
        client = HttpQaClient(args.api_url, api_key=os.getenv("CASCADE_QA_API_KEY", ""))
        result = RepoQaWorkflow(executor, client).run(
            checkout,
            plan,
            fix_mode=args.fix_mode,
            pipeline_id=os.getenv("GITHUB_RUN_ID") or os.getenv("CI_PIPELINE_ID") or "local",
        )
        result_path = Path(args.result).resolve()
        result_path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
        _print_result(result, result_path)
        return 0 if result.passed else 2
    except Exception as exc:
        print(f"ERROR: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if checkout and not args.keep_workspace:
            cleanup_checkout(checkout)
        elif checkout:
            print(f"Kept QA workspace: {checkout.root}")


def _print_plan(checkout: Checkout, plan) -> None:
    print(f"Project: {plan.config.project_name} ({plan.config.project_id})")
    print(f"Revision: {checkout.revision} | branch: {checkout.branch}")
    print(f"Detected stacks: {', '.join(plan.stacks)}")
    print(f"Documentation sources: {len(plan.documentation)} | source snapshots: {len(plan.source_files)}")
    print("Approved command plan:")
    for command in plan.commands:
        network = "dependency-network" if command.requires_network else "offline"
        print(f"  - [{command.phase}] {command.name}: {list(command.argv)} ({command.image}, {network}, {command.origin})")


def _print_result(result, result_path: Path) -> None:
    initial = result.initial_evaluation
    final = result.verification_evaluation or initial
    print(f"Initial QA: {initial.get('status')} - {initial.get('summary')}")
    if result.applied_changes:
        print(f"Applied {len(result.applied_changes)} documentation-grounded proposal(s) in the disposable checkout")
    if result.verification_evaluation:
        print(f"Verification QA: {final.get('status')} - {final.get('summary')}")
    print(f"Final quality gate: {'PASS' if result.passed else 'FAIL'}")
    print(f"Report: {result_path}")


if __name__ == "__main__":
    raise SystemExit(main())
