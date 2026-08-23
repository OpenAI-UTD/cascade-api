import { FormEvent, useState } from "react";
import { apiGet, apiPost } from "../api/client";
import { useChaosCampaignRuns, useChaosCampaigns, useChaosPlans, useChaosPolicy, useChaosRuns, useControlChaosCampaign, useCreateChaosCampaign, useCreateChaosPlan, useDryRunChaos, useLiveDemoStatus, useResilienceScores, useStartChaosCampaign } from "../api/hooks";
import { AlertTriangle, FlaskConical, Radar } from "lucide-react";
import { Badge } from "../components/Badge";
import { EmptyState } from "../components/EmptyState";
import { SafetyFindingsPanel, TargetWorkloadPanel } from "../components/IntelligencePanels";
import { JsonBlock } from "../components/JsonBlock";
import { StatusPanel } from "../components/cards/StatusPanel";
import { DataTable } from "../components/tables/DataTable";

export function ChaosPage() {
  const policy = useChaosPolicy();
  const plans = useChaosPlans({ limit: 20 });
  const runs = useChaosRuns({ limit: 20 });
  const campaigns = useChaosCampaigns({ limit: 20 });
  const campaignRuns = useChaosCampaignRuns({ limit: 20 });
  const scores = useResilienceScores({ limit: 20 });
  const liveStatus = useLiveDemoStatus();
  const create = useCreateChaosPlan();
  const createCampaign = useCreateChaosCampaign();
  const startCampaign = useStartChaosCampaign();
  const pauseCampaign = useControlChaosCampaign("pause");
  const resumeCampaign = useControlChaosCampaign("resume");
  const stopCampaign = useControlChaosCampaign("stop");
  const dryRun = useDryRunChaos();
  const [form, setForm] = useState({ target_service: "", target_namespace: "", experiment_kind: "pod_kill", duration_seconds: 10, objective: "Validate service resilience with dry-run planning" });
  const [campaignForm, setCampaignForm] = useState({ name: "Safe catalogue campaign", target_namespace: "cascade-targets", allowed_services: "catalogue,carts", experiment_kind: "pod_kill", max_experiments_per_run: 2, blast_radius_limit: 0.5, cooldown_seconds: 0 });
  const [confirmed, setConfirmed] = useState(false);
  const [liveResult, setLiveResult] = useState<Record<string, unknown> | undefined>();
  const [liveError, setLiveError] = useState("");
  const [livePending, setLivePending] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const live = liveStatus.data?.chaos;
  const safeServices = (live?.allowed_services ?? []).filter((service) => !(live?.protected_services ?? []).includes(service));
  const selectedService = form.target_service || safeServices[0] || "";
  const selectedNamespace = form.target_namespace || live?.allowed_target_namespace || "cascade-targets";
  const liveReady = Boolean(liveStatus.data?.command_center?.dangerous_actions_enabled && live?.dangerous_actions_enabled && live?.real_chaos_enabled && live?.live_demo_mode && live?.allowed_target_namespace === "cascade-targets");

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate({ ...compact(form), duration_seconds: Number(form.duration_seconds), dry_run: true });
  }

  function submitCampaign(event: FormEvent) {
    event.preventDefault();
    const services = campaignForm.allowed_services.split(",").map((item) => item.trim()).filter(Boolean);
    createCampaign.mutate({
      name: campaignForm.name,
      target_namespace: campaignForm.target_namespace,
      allowed_services: services,
      experiment_templates: services.map((service) => ({
        name: `${campaignForm.experiment_kind} ${service}`,
        experiment_kind: campaignForm.experiment_kind,
        target_service: service,
        duration_seconds: Math.min(Number(form.duration_seconds) || 10, 30),
        dry_run: true,
      })),
      schedule: { trigger: "manual" },
      max_experiments_per_run: Number(campaignForm.max_experiments_per_run),
      blast_radius_limit: Number(campaignForm.blast_radius_limit),
      cooldown_seconds: Number(campaignForm.cooldown_seconds),
      dry_run: true,
      local_demo_execution_enabled: false,
    });
  }

  async function runLiveDemo(event: FormEvent) {
    event.preventDefault();
    setLivePending(true);
    setLiveError("");
    setLiveResult(undefined);
    try {
      const plan = await apiPost<Record<string, unknown>>("/chaos/planner/plans", {
        objective: `Local UI demo bounded pod kill for ${selectedService}`,
        target_service: selectedService,
        target_namespace: selectedNamespace,
        experiment_kind: "pod_kill",
        duration_seconds: Math.min(Number(form.duration_seconds) || 10, 30),
        dry_run: true,
      });
      const planId = String(plan.plan_id ?? "");
      const dry = await apiPost<Record<string, unknown>>("/chaos/executor/runs", { plan_id: planId, dry_run: true, approved: false, observation_window_seconds: 5, trigger_agent_investigation: false });
      const approval = await apiPost<Record<string, unknown>>("/remediation/approval/approvals", { plan_id: planId, decision: "approved", approver: "local-demo-user", approver_role: "developer", reason: "Approved local UI demo real chaos after dry-run", expires_minutes: 30 });
      const approvalId = String((approval.approval as Record<string, unknown> | undefined)?.approval_id ?? approval.approval_id ?? "");
      let approvalStatus: Record<string, unknown> | undefined;
      try {
        approvalStatus = await apiGet<Record<string, unknown>>(`/remediation/approval/plans/${planId}/approval-status`);
      } catch {
        approvalStatus = { status: "unavailable" };
      }
      const run = await apiPost<Record<string, unknown>>("/chaos/executor/runs", { plan_id: planId, approval_id: approvalId, dry_run: false, approved: true, observation_window_seconds: 10, trigger_agent_investigation: false });
      setLiveResult({ plan, dry_run: dry, approval, approval_status: approvalStatus, run });
    } catch (error) {
      setLiveError(error instanceof Error ? error.message : String(error));
    } finally {
      setLivePending(false);
    }
  }

  return (
    <div className="page">
      <div className="page-heading"><div><h2>Chaos</h2><p>Dry-run planning and bounded local live-demo execution when backend policy allows it.</p></div></div>
      <div className={`banner ${liveReady ? "danger" : "warn"}`}><AlertTriangle size={18} />{liveReady ? "LIVE DEMO MODE: bounded pod_kill is available for allowlisted local target services." : "Real chaos is disabled by default. Dry-run planning is available now."}</div>
      <section className="panel mode-panel">
        <div>
          <Badge tone={liveReady ? "bad" : "dry"}>{liveReady ? "Live demo enabled" : "Dry-run mode"}</Badge>
          <p>{liveReady ? "Backend policy allows only bounded pod_kill against safe services in cascade-targets." : "To test real local actions, enable Live Demo Mode on a local kind-cascade cluster."}</p>
        </div>
        {!liveReady ? <code className="command-hint">powershell -ExecutionPolicy Bypass -File .\scripts\enable-ui-live-demo.ps1 -ConfirmLocalKind</code> : null}
        {!liveReady && live?.disabled_reasons?.length ? <ul className="reason-list">{live.disabled_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}
      </section>
      <form className="form-grid two-column" onSubmit={submit}>
        <div className="form-field"><label htmlFor="chaos-service">Target service</label><input id="chaos-service" value={form.target_service} onChange={(e) => setForm({ ...form, target_service: e.target.value })} placeholder="target service" /></div>
        <div className="form-field"><label htmlFor="chaos-kind">Experiment kind</label><select id="chaos-kind" value={form.experiment_kind} onChange={(e) => setForm({ ...form, experiment_kind: e.target.value })}><option>pod_kill</option></select></div>
        <div className="form-field"><label htmlFor="chaos-namespace">Namespace</label><input id="chaos-namespace" value={form.target_namespace} onChange={(e) => setForm({ ...form, target_namespace: e.target.value })} placeholder="namespace" /></div>
        <div className="form-field"><label htmlFor="chaos-duration">Duration seconds</label><input id="chaos-duration" type="number" min={5} max={300} value={form.duration_seconds} onChange={(e) => setForm({ ...form, duration_seconds: Number(e.target.value) })} /></div>
        <div className="form-field full"><label htmlFor="chaos-objective">Objective</label><input id="chaos-objective" value={form.objective} onChange={(e) => setForm({ ...form, objective: e.target.value })} placeholder="objective" /></div>
        <button type="submit" className="full">Create dry-run plan</button>
      </form>
      {create.data ? <div className="state success">Created chaos plan {String(create.data.plan_id ?? "")}</div> : null}
      {create.error ? <div className="state error">{create.error.message}</div> : null}
      <section className="panel">
        <h3>Chaos Campaigns</h3>
        <form className="form-grid two-column" onSubmit={submitCampaign}>
          <div className="form-field"><label htmlFor="campaign-name">Campaign name</label><input id="campaign-name" value={campaignForm.name} onChange={(e) => setCampaignForm({ ...campaignForm, name: e.target.value })} /></div>
          <div className="form-field"><label htmlFor="campaign-services">Allowed services</label><input id="campaign-services" value={campaignForm.allowed_services} onChange={(e) => setCampaignForm({ ...campaignForm, allowed_services: e.target.value })} /></div>
          <div className="form-field"><label htmlFor="campaign-namespace">Namespace</label><input id="campaign-namespace" value={campaignForm.target_namespace} onChange={(e) => setCampaignForm({ ...campaignForm, target_namespace: e.target.value })} /></div>
          <div className="form-field"><label htmlFor="campaign-kind">Template kind</label><select id="campaign-kind" value={campaignForm.experiment_kind} onChange={(e) => setCampaignForm({ ...campaignForm, experiment_kind: e.target.value })}><option>pod_kill</option><option>network_delay</option><option>stress_cpu</option></select></div>
          <div className="form-field"><label htmlFor="campaign-max">Max experiments</label><input id="campaign-max" type="number" min={1} max={20} value={campaignForm.max_experiments_per_run} onChange={(e) => setCampaignForm({ ...campaignForm, max_experiments_per_run: Number(e.target.value) })} /></div>
          <div className="form-field"><label htmlFor="campaign-blast">Blast limit</label><input id="campaign-blast" type="number" min={0} max={1} step={0.05} value={campaignForm.blast_radius_limit} onChange={(e) => setCampaignForm({ ...campaignForm, blast_radius_limit: Number(e.target.value) })} /></div>
          <button type="submit" className="full" disabled={createCampaign.isPending}>Create dry-run campaign</button>
        </form>
        {createCampaign.data ? <div className="state success">Created campaign {String(createCampaign.data.campaign.campaign_id ?? "")}</div> : null}
        {createCampaign.error ? <div className="state error">{createCampaign.error.message}</div> : null}
      </section>
      {liveReady ? (
        <form className="form-grid two-column panel" onSubmit={runLiveDemo}>
          <h3 className="full">Local Live Demo Chaos</h3>
          <div className="form-field"><label htmlFor="live-chaos-service">Safe service</label><select id="live-chaos-service" value={selectedService} onChange={(e) => setForm({ ...form, target_service: e.target.value })}>{safeServices.map((service) => <option key={service}>{service}</option>)}</select></div>
          <ReadOnlyValue label="Namespace" value={selectedNamespace} />
          <ReadOnlyValue label="Experiment" value="pod_kill" />
          <div className="form-field"><label htmlFor="live-chaos-duration">Duration seconds</label><input id="live-chaos-duration" type="number" min={10} max={30} value={Math.min(Number(form.duration_seconds) || 10, 30)} onChange={(e) => setForm({ ...form, duration_seconds: Number(e.target.value) })} /></div>
          <label className="checkbox-field full"><input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />I understand this will mutate my local kind target namespace.</label>
          <button type="submit" className="btn-danger full" disabled={!selectedService || !confirmed || livePending}>Run bounded live chaos</button>
        </form>
      ) : null}
      <div aria-live="polite">{liveError ? <div className="state error">{liveError}</div> : null}{liveResult ? <div className="state success">Live chaos flow completed. Cleanup and recovery status are included in the run result.</div> : null}</div>
      <div className="grid two">
        <TargetWorkloadPanel />
        <StatusPanel title="Safety Policy" loading={policy.isLoading} error={policy.error}><SafetyFindingsPanel policy={policy.data} record={plans.data?.plans?.[0]} dryRun={dryRun.data} /></StatusPanel>
        <StatusPanel title="Raw Safety Policy" loading={policy.isLoading} error={policy.error}><JsonBlock value={policy.data} /></StatusPanel>
        <StatusPanel title="Live Demo Status" loading={liveStatus.isLoading} error={liveStatus.error}><JsonBlock value={liveStatus.data} /></StatusPanel>
        <StatusPanel title="Resilience Scores" loading={scores.isLoading} error={scores.error}>
          <DataTable caption="Resilience scores" rows={scores.data?.scores ?? []} columns={[{ key: "computed_at", label: "Computed", width: "120px" }, { key: "service", label: "Service", width: "130px" }, { key: "resilience_score", label: "Score", width: "100px", render: (row) => <Badge tone={gradeTone(row.grade)}>{formatScore(row.resilience_score, row.grade)}</Badge> }, { key: "explanation", label: "Explanation", render: (row) => {
            const id = String(row.score_id ?? row.service ?? "");
            const text = String(row.explanation ?? "");
            const open = expanded.has(id);
            return <span>{open ? text : `${text.slice(0, 160)}${text.length > 160 ? "..." : ""}`} {text.length > 160 ? <button type="button" className="link-button" onClick={() => setExpanded((current) => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; })}>{open ? "Show less" : "Show more"}</button> : null}</span>;
          } }]} />
        </StatusPanel>
      </div>
      <StatusPanel title="Chaos Plans" loading={plans.isLoading} error={plans.error}>
        <DataTable caption="Chaos plans" rows={plans.data?.plans ?? []} empty={<EmptyState icon={<FlaskConical size={22} />} title="No chaos plans yet" hint="Create a dry-run plan above — plans are scored for blast radius and safety before anything runs." />} columns={[{ key: "created_at", label: "Created", width: "140px" }, { key: "target_service", label: "Service", width: "140px" }, { key: "experiment_kind", label: "Kind", width: "120px" }, { key: "blast_radius_score", label: "Blast", width: "90px", render: (row) => formatPercent(row.blast_radius_score) }, { key: "risk_level", label: "Risk", width: "100px", render: (row) => <Badge tone={riskTone(row.risk_level)}>{String(row.risk_level ?? "-")}</Badge> }, { key: "safety_findings", label: "Safety findings", render: (row) => formatList(row.safety_findings) }, { key: "dryrun", label: "Dry-run", width: "120px", align: "right", render: (row) => <button type="button" className="btn-dry compact" disabled={dryRun.isPending} onClick={() => dryRun.mutate({ plan_id: row.plan_id ?? "", observation_window_seconds: 10, trigger_agent_investigation: false })}>Dry-run -&gt;</button> }]} />
      </StatusPanel>
      <StatusPanel title="Chaos Campaigns" loading={campaigns.isLoading} error={campaigns.error}>
        <DataTable caption="Chaos campaigns" rows={campaigns.data?.campaigns ?? []} empty="No campaigns returned." columns={[
          { key: "name", label: "Name" },
          { key: "status", label: "Status", width: "110px", render: (row) => <Badge tone={statusTone(row.status)}>{String(row.status ?? "-")}</Badge> },
          { key: "target_namespace", label: "Namespace", width: "140px" },
          { key: "allowed_services", label: "Services", render: (row) => Array.isArray(row.allowed_services) ? row.allowed_services.join(", ") : "-" },
          { key: "max_experiments_per_run", label: "Max", width: "70px" },
          { key: "blast_radius_limit", label: "Blast cap", width: "100px", render: (row) => formatPercent(row.blast_radius_limit) },
          { key: "actions", label: "Actions", width: "260px", align: "right", render: (row) => {
            const id = String(row.campaign_id ?? "");
            return <span className="button-row"><button type="button" className="btn-dry compact" onClick={() => startCampaign.mutate({ campaignId: id, dry_run: true, requested_by: "command-center" })}>Start</button><button type="button" className="compact" onClick={() => pauseCampaign.mutate(id)}>Pause</button><button type="button" className="compact" onClick={() => resumeCampaign.mutate(id)}>Resume</button><button type="button" className="compact" onClick={() => stopCampaign.mutate(id)}>Stop</button></span>;
          } },
        ]} />
      </StatusPanel>
      <StatusPanel title="Campaign Runs" loading={campaignRuns.isLoading || startCampaign.isPending} error={campaignRuns.error || startCampaign.error}>
        <DataTable caption="Chaos campaign runs" rows={campaignRuns.data?.runs ?? []} empty="No campaign runs returned." columns={[
          { key: "started_at", label: "Started", width: "140px" },
          { key: "status", label: "Status", width: "150px", render: (row) => <Badge tone={statusTone(row.status)}>{String(row.status ?? "-")}</Badge> },
          { key: "services", label: "Services", render: (row) => Array.isArray(row.services) ? row.services.join(", ") : "-" },
          { key: "experiments_succeeded", label: "Done", width: "70px" },
          { key: "experiments_blocked", label: "Blocked", width: "90px" },
          { key: "error_message", label: "Error" },
        ]} />
        {startCampaign.data ? <JsonBlock value={startCampaign.data.report} /> : null}
      </StatusPanel>
      <StatusPanel title="Chaos Runs" loading={runs.isLoading} error={runs.error}>
        <DataTable caption="Chaos runs" rows={runs.data?.runs ?? []} empty={<EmptyState icon={<Radar size={22} />} title="No chaos runs yet" hint="Dry-run a plan above to record the first experiment. Real execution stays disabled unless live demo mode is enabled." />} columns={[{ key: "started_at", label: "Started" }, { key: "target_service", label: "Service" }, { key: "experiment_kind", label: "Kind" }, { key: "dry_run", label: "Dry-run" }, { key: "cleanup_status", label: "Cleanup" }, { key: "status", label: "Status", render: (row) => <Badge tone={statusTone(row.status)}>{String(row.status ?? "-")}</Badge> }]} />
      </StatusPanel>
      {liveResult ? <StatusPanel title="Latest Live Demo Result"><JsonBlock value={liveResult} /></StatusPanel> : null}
    </div>
  );
}

function ReadOnlyValue({ label, value }: { label: string; value: string }) {
  return <div className="readonly-field"><span>{label}</span><code>{value}</code></div>;
}

function gradeTone(value: unknown): "good" | "warn" | "bad" | "neutral" {
  const grade = String(value ?? "").toUpperCase();
  if (grade === "A" || grade === "B") return "good";
  if (grade === "C") return "warn";
  if (grade === "D" || grade === "F") return "bad";
  return "neutral";
}

function formatScore(score: unknown, grade: unknown) {
  const numeric = Number(score);
  const prefix = Number.isFinite(numeric) ? `${Math.round(numeric <= 1 ? numeric * 100 : numeric)}` : "-";
  return `${prefix} ${String(grade ?? "").toUpperCase() || "-"}`;
}

function formatPercent(value: unknown) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "No data returned.";
  return `${Math.round(number <= 1 ? number * 100 : number)}%`;
}

function formatList(value: unknown) {
  return Array.isArray(value) && value.length ? value.join("; ") : "No data returned.";
}

function compact(value: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== ""));
}

function riskTone(value: unknown): "warn" | "bad" | "neutral" {
  const risk = String(value ?? "").toLowerCase();
  if (risk.includes("high") || risk.includes("critical")) return "bad";
  if (risk.includes("medium")) return "warn";
  return "neutral";
}

function statusTone(value: unknown): "good" | "warn" | "bad" | "info" | "neutral" {
  const status = String(value ?? "").toLowerCase();
  if (status.includes("complete") || status.includes("success")) return "good";
  if (status.includes("fail") || status.includes("error")) return "bad";
  if (status.includes("run") || status.includes("pending") || status.includes("dry")) return "info";
  if (status.includes("warn")) return "warn";
  return "neutral";
}
