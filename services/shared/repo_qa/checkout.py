from __future__ import annotations

import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class CheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class Checkout:
    root: Path
    temporary_root: Path
    source: str
    revision: str
    branch: str


def checkout_repository(
    source: str,
    *,
    revision: str = "",
    workspace_root: Path | None = None,
    allow_remote: bool = False,
) -> Checkout:
    _validate_source(source, allow_remote=allow_remote)
    remote = _is_remote(source)
    clone_source = source if remote else str(Path(source).expanduser().resolve())
    parent = Path(tempfile.mkdtemp(prefix="cascade-repo-qa-", dir=str(workspace_root) if workspace_root else None)).resolve()
    destination = parent / "project"
    try:
        clone_args = ["clone", "--no-tags", "--depth", "1"]
        if not remote:
            clone_args = [
                "-c",
                f"safe.directory={clone_source}",
                "-c",
                f"safe.directory={Path(clone_source) / '.git'}",
                *clone_args,
                "--no-local",
            ]
        _git([*clone_args, "--", clone_source, str(destination)])
        if revision:
            checkout = _git(["-C", str(destination), "checkout", "--detach", revision], check=False)
            if checkout.returncode != 0:
                _git(["-C", str(destination), "fetch", "--depth", "1", "origin", revision])
                _git(["-C", str(destination), "checkout", "--detach", "FETCH_HEAD"])
        commit = _git(["-C", str(destination), "rev-parse", "HEAD"]).stdout.strip()
        branch_result = _git(["-C", str(destination), "branch", "--show-current"], check=False)
        branch = branch_result.stdout.strip() or "detached"
        return Checkout(root=destination, temporary_root=parent, source=source, revision=commit, branch=branch)
    except Exception:
        _rmtree(parent, ignore_errors=True)
        raise


def cleanup_checkout(checkout: Checkout) -> None:
    parent = checkout.temporary_root.resolve()
    if checkout.root.resolve().parent != parent or not parent.name.startswith("cascade-repo-qa-"):
        raise CheckoutError(f"Refusing to clean unexpected checkout path: {parent}")
    _rmtree(parent)


def repository_diff(root: Path) -> str:
    result = _git(["-C", str(root), "diff", "--no-ext-diff", "--binary"], check=False)
    return result.stdout[:200_000]


def _validate_source(source: str, *, allow_remote: bool) -> None:
    if not source or "\x00" in source:
        raise CheckoutError("Repository source is empty or invalid")
    parsed = urlsplit(source)
    remote = _is_remote(source)
    if remote and not allow_remote:
        raise CheckoutError("Remote repository checkout requires --allow-remote")
    if parsed.username or parsed.password:
        raise CheckoutError("Repository URLs must not contain embedded credentials")
    if not remote and not Path(source).expanduser().resolve().exists():
        raise CheckoutError(f"Local repository does not exist: {source}")


def _is_remote(source: str) -> bool:
    parsed = urlsplit(source)
    return parsed.scheme in {"http", "https", "ssh", "git"} or source.startswith("git@")


def _git(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["git", *args], text=True, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckoutError(f"Git command failed to start: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise CheckoutError(f"Git command failed ({result.returncode}): {detail}")
    return result


def _rmtree(path: Path, *, ignore_errors: bool = False) -> None:
    def remove_readonly(function, value, exc_info) -> None:
        try:
            Path(value).chmod(stat.S_IWRITE)
            function(value)
        except OSError:
            if not ignore_errors:
                raise exc_info[1]

    shutil.rmtree(path, ignore_errors=ignore_errors, onerror=remove_readonly)
