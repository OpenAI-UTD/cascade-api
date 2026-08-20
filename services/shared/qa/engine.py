from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

from .schemas import (
    DocumentedFixRule,
    EvaluationRequest,
    EvaluationResult,
    EvidenceRef,
    Finding,
    ProposedChange,
    QualityGate,
    Severity,
    WithheldFix,
)

SEVERITY_ORDER: dict[str, int] = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
GENERIC_RECOMMENDATIONS = {
    "test": "Reproduce the failing test locally, isolate the first failing assertion, and correct the implementation or expectation before rerunning the suite.",
    "lint": "Correct the reported static-analysis violation and rerun the same linter with the project configuration.",
    "build": "Resolve the first build error before addressing follow-on errors, then run a clean build to verify the result.",
    "security": "Review the security finding with the project owner, remove or upgrade the affected code or dependency, and verify with the same scanner.",
    "runtime": "Reproduce the runtime failure in the same environment, correlate logs and metrics around the failure, and verify the documented operating expectations.",
    "other": "Review the attached evidence, reproduce the issue, and add a regression check before merging a correction.",
}


def evaluate(payload: EvaluationRequest) -> EvaluationResult:
    evaluation_id = _evaluation_id(payload)
    findings = _build_findings(payload, evaluation_id)
    proposed_changes, withheld = _propose_changes(payload, findings)
    threshold = SEVERITY_ORDER[payload.fail_on]
    blocking = sum(SEVERITY_ORDER[finding.severity] >= threshold for finding in findings)
    gate = QualityGate(
        passed=blocking == 0,
        fail_on=payload.fail_on,
        blocking_findings=blocking,
        reason=(
            f"No findings at or above {payload.fail_on}."
            if blocking == 0
            else f"{blocking} finding(s) met or exceeded the {payload.fail_on} threshold."
        ),
    )
    if not findings:
        status = "passed"
    elif proposed_changes:
        status = "fixes_proposed"
    else:
        status = "issues_found"
    summary = _summary(payload, findings, proposed_changes, withheld)
    return EvaluationResult(
        evaluation_id=evaluation_id,
        created_at=datetime.now(UTC).isoformat(),
        project=payload.project,
        revision=payload.revision,
        branch=payload.branch,
        mode=payload.mode,
        status=status,
        summary=summary,
        findings=findings,
        proposed_changes=proposed_changes,
        withheld_fixes=withheld,
        quality_gate=gate,
        stats={
            "checks": len(payload.checks),
            "runtime_observations": len(payload.runtime_observations),
            "documentation_sources": len(payload.documentation),
            "findings": len(findings),
            "proposed_changes": len(proposed_changes),
            "withheld_fixes": len(withheld),
        },
    )


def _build_findings(payload: EvaluationRequest, evaluation_id: str) -> list[Finding]:
    findings: list[Finding] = []
    for check in payload.checks:
        if check.status not in {"failed", "error"}:
            continue
        summary = check.output.strip() or f"{check.name} reported {check.status}."
        findings.append(
            _finding(
                payload,
                evaluation_id,
                len(findings),
                title=f"{check.name} {check.status}",
                severity=_check_severity(check.kind, check.status),
                category=check.kind,
                summary=summary,
                evidence=EvidenceRef(source="ci", name=check.name, path=check.file, line=check.line),
            )
        )
    for observation in payload.runtime_observations:
        if observation.status == "healthy":
            continue
        details = [observation.message.strip()]
        if observation.metric:
            details.append(f"{observation.metric}: expected {observation.expected or 'unspecified'}, actual {observation.actual or 'unspecified'}")
        findings.append(
            _finding(
                payload,
                evaluation_id,
                len(findings),
                title=f"Runtime observation {observation.name} is {observation.status}",
                severity="critical" if observation.status == "failed" else "high",
                category="runtime",
                summary="; ".join(part for part in details if part) or f"{observation.name} is {observation.status}.",
                evidence=EvidenceRef(source="runtime", name=observation.name, path=observation.file, line=observation.line),
            )
        )
    return findings


def _finding(
    payload: EvaluationRequest,
    evaluation_id: str,
    index: int,
    *,
    title: str,
    severity: Severity,
    category: str,
    summary: str,
    evidence: EvidenceRef,
) -> Finding:
    haystack = f"{title}\n{summary}".lower()
    rule = _matching_rule(payload.documented_fixes, haystack)
    docs = _rank_documentation(payload, haystack, rule)
    recommendation = rule.recommendation if rule else GENERIC_RECOMMENDATIONS.get(category, GENERIC_RECOMMENDATIONS["other"])
    finding_id = f"{evaluation_id}-f{index + 1:03d}"
    return Finding(
        finding_id=finding_id,
        title=title,
        severity=severity,
        category=category,
        summary=summary[:4000],
        evidence=[evidence],
        documentation_refs=docs,
        recommendation=recommendation,
        matched_rule_id=rule.rule_id if rule else "",
    )


def _propose_changes(payload: EvaluationRequest, findings: list[Finding]) -> tuple[list[ProposedChange], list[WithheldFix]]:
    if payload.mode != "propose_fix":
        return [], []
    rules = {rule.rule_id: rule for rule in payload.documented_fixes}
    docs = {doc.path for doc in payload.documentation}
    sources = {source.path: source.content for source in payload.source_files}
    changes: list[ProposedChange] = []
    withheld: list[WithheldFix] = []
    seen: set[tuple[str, str, str]] = set()
    for finding in findings:
        if not finding.matched_rule_id:
            withheld.append(WithheldFix(rule_id="", finding_id=finding.finding_id, reason="No documented fix rule matched this issue."))
            continue
        rule = rules[finding.matched_rule_id]
        if rule.documentation_path not in docs:
            withheld.append(WithheldFix(rule_id=rule.rule_id, finding_id=finding.finding_id, reason=f"Required documentation was not supplied: {rule.documentation_path}"))
            continue
        if not rule.changes:
            withheld.append(WithheldFix(rule_id=rule.rule_id, finding_id=finding.finding_id, reason="The matching rule recommends a solution but does not define a source change."))
            continue
        for change in rule.changes:
            source = sources.get(change.path)
            if source is None:
                withheld.append(WithheldFix(rule_id=rule.rule_id, finding_id=finding.finding_id, reason=f"Source snapshot was not supplied: {change.path}"))
                continue
            occurrences = source.count(change.original)
            if occurrences != 1:
                withheld.append(WithheldFix(rule_id=rule.rule_id, finding_id=finding.finding_id, reason=f"Expected exactly one source match in {change.path}; found {occurrences}."))
                continue
            key = (rule.rule_id, change.path, change.original)
            if key in seen:
                continue
            seen.add(key)
            changes.append(
                ProposedChange(
                    rule_id=rule.rule_id,
                    finding_id=finding.finding_id,
                    path=change.path,
                    original=change.original,
                    replacement=change.replacement,
                    documentation_path=rule.documentation_path,
                    rationale=rule.recommendation,
                )
            )
    return changes, withheld


def _matching_rule(rules: list[DocumentedFixRule], haystack: str) -> DocumentedFixRule | None:
    matches: list[tuple[int, DocumentedFixRule]] = []
    for rule in rules:
        patterns = [pattern.strip().lower() for pattern in rule.issue_patterns if pattern.strip()]
        matched = [pattern for pattern in patterns if pattern in haystack]
        if matched:
            matches.append((max(len(pattern) for pattern in matched), rule))
    return max(matches, key=lambda item: (item[0], item[1].rule_id))[1] if matches else None


def _rank_documentation(payload: EvaluationRequest, haystack: str, rule: DocumentedFixRule | None) -> list[str]:
    if rule and any(doc.path == rule.documentation_path for doc in payload.documentation):
        return [rule.documentation_path]
    issue_tokens = _tokens(haystack)
    ranked: list[tuple[int, str]] = []
    for doc in payload.documentation:
        score = len(issue_tokens & _tokens(f"{doc.path} {' '.join(doc.tags)} {doc.content}"))
        if score:
            ranked.append((score, doc.path))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in ranked[:3]]


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_.-]{3,}", value.lower()) if token not in {"the", "and", "for", "with", "this", "that", "from"}}


def _check_severity(kind: str, status: str) -> Severity:
    if kind == "security":
        return "critical" if status == "error" else "high"
    if kind in {"build", "runtime"}:
        return "critical" if status == "error" else "high"
    if kind == "test":
        return "high"
    return "medium" if status == "error" else "low"


def _evaluation_id(payload: EvaluationRequest) -> str:
    seed = f"{payload.project.project_id}:{payload.revision}:{datetime.now(UTC).isoformat()}"
    return "qa_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _summary(payload: EvaluationRequest, findings: list[Finding], changes: list[ProposedChange], withheld: list[WithheldFix]) -> str:
    if not findings:
        return f"{payload.project.name} passed the supplied CI and runtime evidence."
    base = f"Found {len(findings)} issue(s) in {payload.project.name}."
    if payload.mode == "propose_fix":
        return f"{base} Proposed {len(changes)} documentation-grounded change(s); withheld {len(withheld)} unsafe or unsupported fix(es)."
    return f"{base} Returned recommendations without proposing source changes."
