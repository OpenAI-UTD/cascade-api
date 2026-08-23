import { FlaskConical, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEvalFixtures, useQaProjects, useProjectEvaluations } from "../api/hooks";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { DataTable } from "../components/tables/DataTable";
import { StatusPanel } from "../components/cards/StatusPanel";

export function EvalsPage() {
  const fixtures = useEvalFixtures();
  const projects = useQaProjects();
  const evalProject = projects.data?.projects?.find((item) => item.project_id)?.project_id;
  const history = useProjectEvaluations(evalProject);

  const evalRows =
    history.data?.evaluations?.flatMap((evaluation) =>
      (evaluation.findings ?? [])
        .filter((finding) => ["affiliation", "agent-authz", "eval"].includes(String(finding.category ?? "")))
        .map((finding) => ({
          evaluation_id: evaluation.evaluation_id,
          created_at: evaluation.created_at,
          category: finding.category,
          title: finding.title,
          severity: finding.severity,
        })),
    ) ?? [];

  return (
    <div className="page evals-page">
      <div className="page-heading">
        <div>
          <h2>Agent Evals</h2>
          <p>Blossom and officer-agent fixtures from the Cascade PROPOSAL — affiliation copy and authz regressions fail the gate.</p>
        </div>
      </div>

      <section className="panel">
        <div className="section-title"><FlaskConical size={18} /> Fixture catalog</div>
        <StatusPanel title="Registered eval scenarios" loading={fixtures.isLoading} error={fixtures.error}>
          <DataTable
            caption="Cascade eval fixtures"
            rows={fixtures.data?.fixtures ?? []}
            empty="No eval fixtures configured."
            columns={[
              { key: "fixture_id", label: "Fixture", render: (row) => <code className="inline-code">{String(row.fixture_id ?? "-")}</code> },
              { key: "title", label: "Scenario" },
              {
                key: "category",
                label: "Category",
                width: "140px",
                render: (row) => (
                  row.category === "agent-authz"
                    ? <Badge tone="warn">agent-authz</Badge>
                    : <Badge tone="neutral">affiliation</Badge>
                ),
              },
              {
                key: "expect_status",
                label: "Expect",
                width: "100px",
                render: (row) => (
                  row.expect_status === "failed"
                    ? <Badge tone="bad">blocked</Badge>
                    : <Badge tone="good">pass</Badge>
                ),
              },
            ]}
          />
        </StatusPanel>
      </section>

      <section className="panel">
        <div className="section-title"><ShieldAlert size={18} /> Recent eval regressions {evalProject ? `· ${evalProject}` : ""}</div>
        <StatusPanel title="Affiliation and agent-authz findings" loading={history.isLoading} error={history.error}>
          <DataTable
            caption="Eval-related findings"
            rows={evalRows}
            empty={(
              <EmptyState
                icon={<ShieldCheck size={22} />}
                title="No eval regressions recorded yet"
                hint="Submit eval checks from CI or run `python scripts/cascade_cli.py eval . --in-process-qa` to see affiliation and agent-authz findings here."
              />
            )}
            columns={[
              { key: "created_at", label: "When", width: "180px", render: (row) => shortTime(row.created_at) },
              { key: "category", label: "Category", width: "120px" },
              { key: "severity", label: "Severity", width: "100px", render: (row) => <Badge tone={row.severity === "high" || row.severity === "critical" ? "bad" : "warn"}>{String(row.severity ?? "-")}</Badge> },
              { key: "title", label: "Finding" },
            ]}
          />
        </StatusPanel>
      </section>
    </div>
  );
}

function shortTime(value: unknown): string {
  if (!value) return "-";
  const date = new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
