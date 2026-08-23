import { FormEvent, useRef, useState } from "react";
import { Bot, ShieldCheck } from "lucide-react";
import { useAutopilotMode, useAutopilotRun, useAutopilotRuns, useCreateAutopilotRun } from "../api/hooks";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { JsonBlock } from "../components/JsonBlock";
import { StatusPanel } from "../components/cards/StatusPanel";
import { DataTable } from "../components/tables/DataTable";

export function AutopilotPage() {
  const mode = useAutopilotMode();
  const runs = useAutopilotRuns({ limit: 30 });
  const create = useCreateAutopilotRun();
  const [selected, setSelected] = useState("");
  const detail = useAutopilotRun(selected);
  const detailRef = useRef<HTMLElement | null>(null);
  const [form, setForm] = useState({
    trigger_type: "latest_anomaly",
    trigger_id: "",
    service: "",
    namespace: "cascade-targets",
    objective: "Run a safe Autopilot reliability loop",
    mode: "dry_run",
    preferred_action_type: "investigate_only",
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate(compact(form), {
      onSuccess: (data) => {
        const id = String(data.run?.run_id ?? "");
        setSelected(id);
        window.requestAnimationFrame(() => detailRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
      },
    });
  }

  return (
    <div className="page">
      <div className="page-heading">
        <div>
          <h2>Autopilot</h2>
          <p>Deterministic detect, investigate, recommend, dry-run, and verify orchestration with safety gates.</p>
        </div>
      </div>

      <section className="panel mode-panel">
        <div>
          <Badge tone={modeTone(mode.data?.default_mode)}>{String(mode.data?.default_mode ?? "dry_run")}</Badge>
          <p>Autopilot composes existing Cascade services. Real execution remains blocked unless local demo remediation policy allows it and approval is current.</p>
        </div>
        <div className="autopilot-mode-card">
          <ShieldCheck size={18} />
          <span>{mode.data?.safe_by_default === false ? "Safety policy unavailable" : "Safe by default"}</span>
          <code>{availableModes(mode.data?.available_modes)}</code>
        </div>
      </section>

      <form className="form-grid two-column" onSubmit={submit}>
        <div className="form-field">
          <label htmlFor="autopilot-trigger">Trigger</label>
          <select id="autopilot-trigger" value={form.trigger_type} onChange={(e) => setForm({ ...form, trigger_type: e.target.value })}>
            <option>latest_anomaly</option>
            <option>anomaly</option>
            <option>service</option>
            <option>manual</option>
          </select>
        </div>
        <div className="form-field">
          <label htmlFor="autopilot-mode">Mode</label>
          <select id="autopilot-mode" value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}>
            <option>dry_run</option>
            <option>read_only</option>
            <option>local_demo_execute</option>
          </select>
        </div>
        <div className="form-field">
          <label htmlFor="autopilot-service">Service</label>
          <input id="autopilot-service" value={form.service} onChange={(e) => setForm({ ...form, service: e.target.value })} placeholder="optional" />
        </div>
        <div className="form-field">
          <label htmlFor="autopilot-namespace">Namespace</label>
          <input id="autopilot-namespace" value={form.namespace} onChange={(e) => setForm({ ...form, namespace: e.target.value })} />
        </div>
        <div className="form-field">
          <label htmlFor="autopilot-trigger-id">Trigger ID</label>
          <input id="autopilot-trigger-id" value={form.trigger_id} onChange={(e) => setForm({ ...form, trigger_id: e.target.value })} placeholder="required for anomaly trigger" />
        </div>
        <div className="form-field">
          <label htmlFor="autopilot-action">Preferred action</label>
          <select id="autopilot-action" value={form.preferred_action_type} onChange={(e) => setForm({ ...form, preferred_action_type: e.target.value })}>
            <option>investigate_only</option>
            <option>restart_deployment</option>
            <option>scale_deployment_noop</option>
          </select>
        </div>
        <div className="form-field full">
          <label htmlFor="autopilot-objective">Objective</label>
          <input id="autopilot-objective" value={form.objective} onChange={(e) => setForm({ ...form, objective: e.target.value })} />
        </div>
        <button type="submit" className="full" disabled={create.isPending}>{create.isPending ? <span className="spinner" /> : <Bot size={16} />}{create.isPending ? "Running..." : "Start Autopilot run"}</button>
      </form>

      <div aria-live="polite">{create.error ? <div className="state error">{create.error.message}</div> : null}{create.data ? <div className="state success">Autopilot run {String(create.data.run?.run_id ?? "")} recorded with state {String(create.data.run?.status ?? "")}.</div> : null}</div>

      <StatusPanel title="Recent Autopilot Runs" loading={runs.isLoading} error={runs.error}>
        <DataTable
          caption="Recent Autopilot runs"
          rows={runs.data?.runs ?? []}
          empty={<EmptyState icon={<Bot size={20} />} title="No Autopilot runs yet" hint="Start a run with the form above — Autopilot investigates an anomaly or service, proposes a policy-gated plan, and records each step." />}
          getRowClassName={(row) => String(row.run_id ?? "") === selected ? "row-selected" : ""}
          onRowClick={(row) => setSelected(String(row.run_id ?? ""))}
          columns={[
            { key: "created_at", label: "Created", width: "135px" },
            { key: "status", label: "State", width: "150px", render: (row) => <Badge tone={statusTone(row.status)}>{String(row.status ?? "-")}</Badge> },
            { key: "final_result", label: "Result", width: "120px", render: (row) => <Badge tone={statusTone(row.final_result)}>{String(row.final_result || row.status || "-")}</Badge> },
            { key: "mode", label: "Mode", width: "110px", render: (row) => <Badge tone={modeTone(row.mode)}>{String(row.mode ?? "-")}</Badge> },
            { key: "service", label: "Service", width: "130px" },
            { key: "proposed_action", label: "Action", width: "160px" },
            { key: "remediation_plan_id", label: "Plan", width: "170px" },
          ]}
        />
      </StatusPanel>

      <section ref={detailRef} className="detail-anchor" aria-live="polite">
        <StatusPanel title={selected ? `Autopilot Detail: ${selected}` : "Autopilot Detail"} loading={detail.isLoading} error={detail.error}>
          {selected && detail.data ? <AutopilotDetail value={detail.data} /> : <div className="state">Select an Autopilot run to inspect evidence, recommendation, action, verification, and step history.</div>}
        </StatusPanel>
      </section>
    </div>
  );
}

function AutopilotDetail({ value }: { value: { run?: Record<string, unknown>; steps?: Record<string, unknown>[] } }) {
  const run = value.run ?? {};
  const steps = value.steps ?? [];
  return (
    <div className="autopilot-detail">
      <div className="autopilot-state-strip large">
        {autopilotStates(run, steps).map((item) => <span className={item.done ? "done" : item.blocked ? "blocked" : ""} key={item.label}>{item.label}</span>)}
      </div>
      <div className="insight-rows">
        <div><span>Evidence</span><strong>{summaryText(run.evidence)}</strong></div>
        <div><span>Recommendation</span><strong>{String(run.proposed_action ?? "No proposed action")}</strong></div>
        <div><span>Verification</span><strong>{summaryText(run.verification)}</strong></div>
      </div>
      <div className="grid two">
        <section className="detail-card"><h3>Evidence / Recommendation / Action / Verification</h3><JsonBlock value={{ evidence: run.evidence, recommendation: run.recommendation, action: run.action, verification: run.verification }} /></section>
        <section className="detail-card"><h3>Steps</h3>{steps.length ? <div className="timeline">{steps.map((step, index) => <details key={String(step.step_id ?? index)} open={index === steps.length - 1}><summary><span>{index + 1}</span><strong>{String(step.state ?? "step")}</strong><Badge tone={statusTone(step.status)}>{String(step.status ?? "recorded")}</Badge></summary><JsonBlock value={step} /></details>)}</div> : <div className="state">No steps returned.</div>}</section>
      </div>
    </div>
  );
}

function compact(value: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== ""));
}

function availableModes(value: unknown) {
  return Array.isArray(value) ? value.join(", ") : "read_only, dry_run, local_demo_execute";
}

function summaryText(value: unknown) {
  if (!value || typeof value !== "object") return "No data returned";
  const record = value as Record<string, unknown>;
  return String(record.summary ?? record.decision ?? record.dry_run_validation ?? JSON.stringify(record).slice(0, 120));
}

function autopilotStates(run: Record<string, unknown>, steps: Record<string, unknown>[]) {
  const joined = steps.map((step) => `${String(step.state ?? "")} ${String(step.summary ?? "")}`).join(" ").toLowerCase();
  const final = String(run.final_result ?? run.status ?? "").toLowerCase();
  return [
    { label: "investigated", done: Boolean(run.investigation_id || joined.includes("investigat")) },
    { label: "planned", done: Boolean(run.remediation_plan_id || joined.includes("plan")) },
    { label: "policy checked", done: Boolean(joined.includes("policy") || run.action) },
    { label: "dry-run", done: Boolean(run.dry_run_execution_id || joined.includes("dry")) },
    { label: final.includes("blocked") ? "blocked" : "executed", done: Boolean(run.execution_id), blocked: final.includes("blocked") || joined.includes("blocked") },
    { label: "verified", done: Boolean(run.verification || joined.includes("verif")) },
  ];
}

function modeTone(value: unknown): "good" | "warn" | "bad" | "info" | "neutral" | "dry" {
  const mode = String(value ?? "").toLowerCase();
  if (mode === "read_only") return "good";
  if (mode === "dry_run") return "dry";
  if (mode === "local_demo_execute") return "bad";
  return "neutral";
}

function statusTone(value: unknown): "good" | "warn" | "bad" | "info" | "neutral" {
  const status = String(value ?? "").toLowerCase();
  if (status === "fixed" || status.includes("complete")) return "good";
  if (status === "failed" || status.includes("error")) return "bad";
  if (status === "degraded" || status.includes("approval")) return "warn";
  if (status === "unchanged" || status.includes("run") || status.includes("ing") || status.includes("requested")) return "info";
  return "neutral";
}
