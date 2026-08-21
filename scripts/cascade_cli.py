#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _api_base() -> str:
    return os.getenv("CASCADE_QA_API_URL", "http://localhost:8040").rstrip("/")


def _api_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "cascade-cli/0.1"}
    api_key = os.getenv("CASCADE_QA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _api_get(path: str) -> dict:
    request = Request(f"{_api_base()}{path}", headers=_api_headers(), method="GET")
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _api_post(path: str, payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload or {}).encode("utf-8")
    headers = {**_api_headers(), "Content-Type": "application/json"}
    request = Request(f"{_api_base()}{path}", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=60) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API error {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SystemExit(f"API request failed: {exc}") from exc


def cmd_health(_: argparse.Namespace) -> int:
    payload = _api_get("/health")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") == "ok" else 1


def cmd_projects(args: argparse.Namespace) -> int:
    if args.action == "list":
        payload = _api_get(f"/projects?limit={args.limit}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.action == "register":
        if not args.project_id or not args.name:
            raise SystemExit("register requires --project-id and --name")
        _, payload = _api_post(
            "/projects/register",
            {
                "project_id": args.project_id,
                "name": args.name,
                "team": args.team,
                "visibility": args.visibility,
                "quota_runs_per_day": args.quota,
                "repository": args.repository,
            },
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    raise SystemExit(f"unknown projects action: {args.action}")


def cmd_eval(args: argparse.Namespace) -> int:
    import importlib.util

    runner_path = REPOSITORY_ROOT / "services" / "repo-qa-runner" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("repo_qa_runner_main", runner_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"runner not found: {runner_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    argv = [
        str(Path(args.path).resolve()),
        "--result",
        args.result,
        "--fix-mode",
        args.fix_mode,
    ]
    if args.executor == "local":
        argv.extend(["--executor", "local", "--allow-local-execution"])
    if args.in_process or args.in_process_qa:
        argv.append("--in-process-qa")
    else:
        argv.extend(["--api-url", args.api_url or _api_base()])
    if args.allow_project_commands:
        argv.append("--allow-project-commands")
    if args.allow_dependency_network:
        argv.append("--allow-dependency-network")
    if args.pull_images:
        argv.append("--pull-images")
    if args.revision:
        argv.extend(["--revision", args.revision])
    return int(module.main(argv))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cascade", description="Cascade QA CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Check Project QA API health")
    health.set_defaults(func=cmd_health)

    projects = sub.add_parser("projects", help="List or register QA projects")
    projects.add_argument("action", choices=["list", "register"], default="list", nargs="?")
    projects.add_argument("--limit", type=int, default=50)
    projects.add_argument("--project-id")
    projects.add_argument("--name")
    projects.add_argument("--team", default="openai-utd")
    projects.add_argument("--visibility", default="club", choices=["club", "officers", "members", "public"])
    projects.add_argument("--quota", type=int, default=100)
    projects.add_argument("--repository", default="")
    projects.set_defaults(func=cmd_projects)

    eval_cmd = sub.add_parser("eval", help="Run repo QA against a project checkout")
    eval_cmd.add_argument("path", help="Project repository path")
    eval_cmd.add_argument("--api-url", default="")
    eval_cmd.add_argument("--result", default="cascade-qa-result.json")
    eval_cmd.add_argument("--fix-mode", choices=["recommend", "propose", "apply"], default="recommend")
    eval_cmd.add_argument("--executor", choices=["docker", "local"], default="local")
    eval_cmd.add_argument("--in-process", action="store_true", help="Use InProcessQaClient (alias: --in-process-qa)")
    eval_cmd.add_argument("--in-process-qa", action="store_true")
    eval_cmd.add_argument("--allow-project-commands", action="store_true", default=True)
    eval_cmd.add_argument("--allow-dependency-network", action="store_true")
    eval_cmd.add_argument("--pull-images", action="store_true")
    eval_cmd.add_argument("--revision", default="")
    eval_cmd.set_defaults(func=cmd_eval)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
