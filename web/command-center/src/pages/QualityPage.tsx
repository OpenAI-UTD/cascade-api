import { useState } from "react";
import { BadgeCheck, FolderKanban } from "lucide-react";
import { useQaProjects, useProjectEvaluations } from "../api/hooks";
import { Badge } from "../components/Badge";
import { DataTable } from "../components/tables/DataTable";
import { StatusPanel } from "../components/cards/StatusPanel";

export function QualityPage() {
  const projects = useQaProjects();
  const [selected, setSelected] = useState<string>("");
  const activeProject = selected || projects.data?.projects?.[0]?.project_id || "";
  const history = useProjectEvaluations(activeProject || undefined);

  return (
    <div className="page quality-page">
      <div className="page-heading">
        <div>
          <h2>Club Quality</h2>
          <p>Cascade QA gates for OpenAI Club at UT Dallas repos — affiliation lint, tests, and agent-authz evals.</p>
        </div>
      </div>

      <section className="panel">
        <div className="section-title"><FolderKanban size={18} /> Projects</div>
        <StatusPanel title="Registered projects" loading={projects.isLoading} error={projects.error}>
          <div className="quality-toolbar">
            <label htmlFor="qa-project-select">
              Project
              <select
                id="qa-project-select"
                value={activeProject}
                onChange={(event) => setSelected(event.target.value)}
                disabled={!projects.data?.projects?.length}
              >
                {(projects.data?.projects ?? []).map((project) => (
                  <option key={project.project_id} value={project.project_id}>
                    {project.name ? `${project.name} (${project.project_id})` : project.project_id}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <DataTable
            caption="Club QA projects"
            rows={projects.data?.projects ?? []}
            empty="No QA runs yet. Wire Cascade QA in CI to populate project history."
            columns={[
              { key: "project_id", label: "Project", render: (row) => <code className="inline-code">{String(row.project_id ?? "-")}</code> },
              { key: "team", label: "Team", width: "120px", render: (row) => String(row.team ?? "-") },
              { key: "visibility", label: "Visibility", width: "110px", render: (row) => String(row.visibility ?? "-") },
              { key: "last_run_at", label: "Last run", width: "180px", render: (row) => shortTime(row.last_run_at) },
              { key: "run_count", label: "Runs", width: "80px" },
              {
                key: "gate_passed",
                label: "Gate",
                width: "120px",
                render: (row) => (
                  row.gate_passed == null
                    ? <Badge tone="neutral">unknown</Badge>
                    : row.gate_passed
                      ? <Badge tone="good">pass</Badge>
                      : <Badge tone="bad">fail</Badge>
                ),
              },
              { key: "findings", label: "Findings", width: "90px" },
              { key: "quota_remaining", label: "Quota left", width: "100px", render: (row) => String(row.quota_remaining ?? "-") },
            ]}
          />
        </StatusPanel>
      </section>

      <section className="panel">
        <div className="section-title"><BadgeCheck size={18} /> Run history {activeProject ? `· ${activeProject}` : ""}</div>
        <StatusPanel title="Recent evaluations" loading={history.isLoading} error={history.error}>
          <DataTable
            caption="Recent QA evaluations"
            rows={history.data?.evaluations ?? []}
            empty={activeProject ? "No evaluations recorded for this project." : "Select a project with runs to inspect history."}
            columns={[
              { key: "created_at", label: "Created", width: "180px", render: (row) => shortTime(row.created_at) },
              { key: "status", label: "Status", width: "120px", render: (row) => <Badge tone={row.status === "passed" ? "good" : "warn"}>{String(row.status ?? "-")}</Badge> },
              {
                key: "quality_gate",
                label: "Gate",
                width: "120px",
                render: (row) => {
                  const gate = row.quality_gate as { passed?: boolean; blocking_findings?: number } | undefined;
                  return gate?.passed ? <Badge tone="good">pass</Badge> : <Badge tone="bad">{gate?.blocking_findings ?? 0} blocking</Badge>;
                },
              },
              { key: "summary", label: "Summary" },
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
