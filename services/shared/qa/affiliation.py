from __future__ import annotations

import re
from typing import Any

from .schemas import DocumentationInput, SourceFile

FORBIDDEN_IDENTITY = re.compile(
    r"\b(?:we\s+are\s+openai|i\s+am\s+openai|official\s+openai\s+(?:website|product|service))\b",
    re.IGNORECASE,
)
NEGATION_HINT = re.compile(r"\b(?:never|do\s+not|does\s+not|don't|not\s+say|cannot\s+say|must\s+not)\b", re.IGNORECASE)
REQUIRED_CLUB_NAME = re.compile(r"openai\s+club\s+at\s+ut\s+dallas", re.IGNORECASE)
GENERIC_AI_GRADIENT = re.compile(r"#(?:7c3aed|8b5cf6|6366f1|a855f7)\b", re.IGNORECASE)
SECRET_PATTERNS = [
    re.compile(r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-./+=]{12,}['\"]", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
]
LICENSE_HINTS = re.compile(r"\b(?:mit license|apache license|bsd license|copyright)\b", re.IGNORECASE)
README_NAMES = {"readme.md", "readme"}


def affiliation_findings(
    source_files: list[SourceFile],
    documentation: list[DocumentationInput],
    *,
    evaluation_id: str,
    start_index: int = 0,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    index = start_index
    scanned = _scan_targets(source_files, documentation)
    for target in scanned:
        path = target["path"]
        content = target["content"]
        basename = path.rsplit("/", 1)[-1].lower()
        is_public_readme = basename in README_NAMES

        for match in FORBIDDEN_IDENTITY.finditer(content):
            line_start = content.rfind("\n", 0, match.start()) + 1
            line_end = content.find("\n", match.start())
            line = content[line_start : line_end if line_end != -1 else len(content)]
            if NEGATION_HINT.search(line):
                continue
            findings.append(
                _affiliation_finding(
                    evaluation_id,
                    index,
                    title="Affiliation copy claims OpenAI company identity",
                    severity="high",
                    summary=f"Remove corporate identity phrasing near `{match.group(0)}`.",
                    path=path,
                    line=_line_number(content, match.start()),
                    recommendation='Use "OpenAI Club at UT Dallas" — never claim to be OpenAI Inc.',
                )
            )
            index += 1

        if is_public_readme and not REQUIRED_CLUB_NAME.search(content):
            findings.append(
                _affiliation_finding(
                    evaluation_id,
                    index,
                    title="Public README missing club affiliation line",
                    severity="medium",
                    summary="Public READMEs must name the OpenAI Club at UT Dallas.",
                    path=path,
                    recommendation='Add "OpenAI Club at UT Dallas" to the README introduction.',
                )
            )
            index += 1

        if GENERIC_AI_GRADIENT.search(content):
            findings.append(
                _affiliation_finding(
                    evaluation_id,
                    index,
                    title="Generic purple-AI brand color detected",
                    severity="low",
                    summary="Prefer UTD green #154734 and club orange #e87500 over generic AI gradients.",
                    path=path,
                    recommendation="Replace generic purple AI palette tokens with club brand colors.",
                )
            )
            index += 1

        for pattern in SECRET_PATTERNS:
            match = pattern.search(content)
            if match:
                findings.append(
                    _affiliation_finding(
                        evaluation_id,
                        index,
                        title="Possible committed secret",
                        severity="critical",
                        summary=f"Secret-like value detected in {path}.",
                        path=path,
                        line=_line_number(content, match.start()),
                        recommendation="Remove the secret, rotate credentials, and use environment variables.",
                    )
                )
                index += 1
                break

        if is_public_readme and not LICENSE_HINTS.search(content):
            findings.append(
                _affiliation_finding(
                    evaluation_id,
                    index,
                    title="Public README missing license notice",
                    severity="low",
                    summary="Include license or copyright notice on public README files.",
                    path=path,
                    recommendation="Add a license section or link to LICENSE in the README.",
                )
            )
            index += 1

    return findings


def _scan_targets(
    source_files: list[SourceFile],
    documentation: list[DocumentationInput],
) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = [{"path": item.path, "content": item.content} for item in source_files]
    for doc in documentation:
        if doc.path.lower().endswith(".md") or doc.path.lower().startswith("readme"):
            targets.append({"path": doc.path, "content": doc.content})
    return targets


def _affiliation_finding(
    evaluation_id: str,
    index: int,
    *,
    title: str,
    severity: str,
    summary: str,
    path: str,
    recommendation: str,
    line: int | None = None,
) -> dict[str, Any]:
    return {
        "finding_id": f"{evaluation_id}-aff-{index}",
        "title": title,
        "severity": severity,
        "category": "affiliation",
        "summary": summary,
        "evidence": [{"source": "documentation", "name": "affiliation-lint", "path": path, "line": line}],
        "documentation_refs": ["cascade-api/PROPOSAL.md#affiliation-lint"],
        "recommendation": recommendation,
        "matched_rule_id": "",
    }


def _line_number(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1
