from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_index_contains_no_gitlink_for_online_boutique_src() -> None:
    """Fresh clones must not get a gitlink without .gitmodules (mode 160000)."""
    entries = [
        line.split()[1]
        for line in _git("ls-files", "-s", "targets/online-boutique-src").splitlines()
        if line.strip()
    ]
    assert all(mode != "160000" for mode in entries), (
        "targets/online-boutique-src is tracked as a gitlink; add .gitmodules "
        "or remove the gitlink and track a README placeholder instead"
    )


def test_online_boutique_placeholder_readme_is_tracked() -> None:
    tracked = _git("ls-files", "targets/online-boutique-src").splitlines()
    assert "targets/online-boutique-src/README.md" in tracked
    readme = ROOT / "targets" / "online-boutique-src" / "README.md"
    assert readme.is_file()
    content = readme.read_text(encoding="utf-8")
    assert "microservices-demo" in content  # upstream fetch instructions present


def test_no_tracked_submodule_gitlinks_without_gitmodules() -> None:
    """Any mode-160000 entry in the index requires a matching .gitmodules path."""
    if not (ROOT / ".gitmodules").exists():
        gitlinks = [
            line
            for line in _git("ls-files", "-s").splitlines()
            if line.startswith("160000 ")
        ]
        assert not gitlinks, f"gitlinks without .gitmodules: {gitlinks}"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("examples/demo/__pycache__/mod.cpython-312.pyc", True),
        ("examples/deep/nested/__pycache__/x.pyc", True),
        ("services/evals-service/__pycache__/x.pyc", True),
        ("tests/__pycache__/test_x.cpython-312-pytest-9.0.2.pyc", True),
        ("examples/demo/main.py", False),
    ],
)
def test_pycache_ignored_everywhere_including_negated_examples(path: str, expected: bool) -> None:
    """`!examples/**` re-includes example files but never their __pycache__."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    ignored = result.returncode == 0
    assert ignored is expected, f"{path}: ignored={ignored}, expected={expected}"


def test_working_tree_has_no_untracked_pycache_files() -> None:
    status = _git("status", "--porcelain")
    leaks = [line for line in status.splitlines() if "__pycache__" in line]
    assert leaks == [], f"untracked __pycache__ leaked into git status: {leaks}"
