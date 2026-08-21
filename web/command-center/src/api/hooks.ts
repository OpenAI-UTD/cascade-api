import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, refreshIntervalMs } from "./client";
import type {
  Anomaly,
  AuditEvent,
  AutopilotRun,
  AutopilotStep,
  Approval,
  ChaosPlan,
  ChaosCampaign,
  ChaosCampaignRun,
  ChaosRun,
  CountMap,
  BlastRadius,
  CausalReport,
  ExperimentEvent,
  FeatureWindow,
  HealthCheck,
  Incident,
  Investigation,
  JsonRecord,
  KnowledgeResult,
  LiveDemoStatus,
  PolicyAuditRecord,
  RemediationExecution,
  RemediationPlan,
  RemediationRollbackPlan,
  RemediationVerification,
  ResilienceScore,
  RcaReport,
  SchedulerDecision,
  SchedulerItem,
  SchedulerStatus,
  TargetWorkload,
  TelemetryEvent,
  TopologyGraph,
  QaProjectSummary,
  QaEvaluation,
  QaEvalFixture,
} from "./types";

const poll = { refetchInterval: refreshIntervalMs };

export function useCounts() {
  return useQuery({ queryKey: ["counts"], queryFn: () => apiGet<CountMap>("/retrieval/debug/counts"), ...poll });
}

export function useTelemetry(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["telemetry", filters], queryFn: () => apiGet<{ events: TelemetryEvent[]; count: number }>("/retrieval/events/recent", filters), ...poll });
}

export function useAuditEvents(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["audit-events", filters], queryFn: () => apiGet<{ events: AuditEvent[]; count: number }>("/retrieval/audit/events", filters), ...poll });
}

export function useAuditTimeline(correlationId?: string) {
  return useQuery({
    queryKey: ["audit-timeline", correlationId],
    enabled: Boolean(correlationId),
    queryFn: () => apiGet<{ events: AuditEvent[]; count: number; correlation_id: string }>("/retrieval/audit/timeline", { correlation_id: correlationId }),
    ...poll,
  });
}

export function useExperiments(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["experiments", filters], queryFn: () => apiGet<{ experiments: ExperimentEvent[]; count: number }>("/retrieval/experiments/recent", filters), ...poll });
}

export function useFeatureWindows(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["features", filters], queryFn: () => apiGet<{ features: FeatureWindow[]; count: number }>("/retrieval/features/recent", filters), ...poll });
}

export function useAnomalies(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["anomalies", filters], queryFn: () => apiGet<{ anomalies: Anomaly[]; count: number }>("/retrieval/anomalies/recent", filters), ...poll });
}

export function useIncidents(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["incidents", filters], queryFn: () => apiGet<{ incidents: Incident[]; count: number }>("/retrieval/incidents/recent", filters), ...poll });
}

export function useIncident(id?: string) {
  return useQuery({ queryKey: ["incident", id], enabled: Boolean(id), queryFn: () => apiGet<JsonRecord>(`/retrieval/incidents/${id}`) });
}

export function useKnowledgeStats() {
  return useQuery({ queryKey: ["knowledge-stats"], queryFn: () => apiGet<JsonRecord>("/knowledge/knowledge/stats"), ...poll });
}

export function useKnowledgeSearch() {
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<{ results: KnowledgeResult[]; evidence_chunks?: KnowledgeResult[]; count?: number; deterministic_summary?: string }>("/knowledge/knowledge/search", body),
  });
}

export function useKnowledgeContext() {
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/knowledge/knowledge/context", body),
  });
}

export function useTopologyGraph() {
  return useQuery({ queryKey: ["topology-graph"], queryFn: () => apiGet<TopologyGraph>("/topology/graph"), ...poll });
}

export function useTopologySnapshot() {
  return useQuery({ queryKey: ["topology-snapshot"], queryFn: () => apiGet<{ snapshot?: TopologyGraph | null }>("/retrieval/topology/snapshot/latest"), ...poll });
}

export function useTargetWorkload() {
  return useQuery({
    queryKey: ["target-workload"],
    queryFn: () => apiGet<TargetWorkload>("/target/target/workload"),
    ...poll,
  });
}

export function useBlastRadius(rootService?: string) {
  return useQuery({
    queryKey: ["blast-radius", rootService],
    enabled: Boolean(rootService),
    queryFn: () => apiPost<BlastRadius>("/topology/topology/blast-radius", { root_service: rootService }),
    ...poll,
  });
}

export function useAnalyzeBlastRadius() {
  return useMutation({
    mutationFn: (body: { root_service: string } & JsonRecord) => apiPost<BlastRadius>("/topology/topology/blast-radius", body),
  });
}

export function useRefreshTopology() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<TopologyGraph>("/topology/topology/refresh", {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["topology-graph"] });
      qc.invalidateQueries({ queryKey: ["target-workload"] });
    },
  });
}

export function useCausalIncidents() {
  return useQuery({ queryKey: ["causal-reports"], queryFn: () => apiGet<{ reports: CausalReport[]; count: number }>("/causality/causality/reports/recent"), ...poll });
}

export function useCausalIncident(id?: string) {
  return useQuery({ queryKey: ["causal-report", id], enabled: Boolean(id), queryFn: () => apiGet<{ report: CausalReport; candidates?: JsonRecord[] }>(`/causality/causality/reports/${id}`) });
}

export function useAnalyzeCausality() {
  return useMutation({
    mutationFn: (body: { target_service?: string } & JsonRecord) => apiPost<{ report: CausalReport }>("/causality/causality/analyze", body),
  });
}

export function useRcaReports(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["rca-reports", filters], queryFn: () => apiGet<{ reports: RcaReport[]; count: number }>("/causality/rca/recent", filters), ...poll });
}

export function useAnalyzeRca() {
  return useMutation({
    mutationFn: (body: { target_service?: string } & JsonRecord) => apiPost<{ report: RcaReport }>("/causality/rca/analyze", body),
  });
}

export function useCausalReport() {
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<CausalReport>("/timeline/report", body),
  });
}

export function usePolicyAuditRecords(enabled = false) {
  return useQuery({
    queryKey: ["policy-audit-records"],
    enabled,
    queryFn: () => apiGet<{ records?: PolicyAuditRecord[]; audits?: PolicyAuditRecord[]; count?: number }>("/remediation/executor/safety/audit"),
    ...poll,
  });
}

export function useInvestigations(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["investigations", filters], queryFn: () => apiGet<{ investigations: Investigation[]; count: number }>("/agent/investigations", filters), ...poll });
}

export function useInvestigation(id?: string) {
  return useQuery({ queryKey: ["investigation", id], enabled: Boolean(id), queryFn: () => apiGet<JsonRecord>(`/agent/investigations/${id}`) });
}

export function useCreateInvestigation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/agent/investigations", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["investigations"] }),
  });
}

export function useChaosPlans(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["chaos-plans", filters], queryFn: () => apiGet<{ plans: ChaosPlan[]; count: number }>("/chaos/planner/plans", filters), ...poll });
}

export function useChaosRuns(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["chaos-runs", filters], queryFn: () => apiGet<{ runs: ChaosRun[]; count: number }>("/chaos/executor/runs", filters), ...poll });
}

export function useResilienceScores(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["resilience-scores", filters], queryFn: () => apiGet<{ scores: ResilienceScore[]; count: number }>("/chaos/executor/scores/recent", filters), ...poll });
}

export function useChaosCampaigns(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["chaos-campaigns", filters], queryFn: () => apiGet<{ campaigns: ChaosCampaign[]; count: number }>("/chaos/planner/campaigns", filters), ...poll });
}

export function useChaosCampaignRuns(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["chaos-campaign-runs", filters], queryFn: () => apiGet<{ runs: ChaosCampaignRun[]; count: number }>("/chaos/planner/campaign-runs", filters), ...poll });
}

export function useChaosPolicy() {
  return useQuery({ queryKey: ["chaos-policy"], queryFn: () => apiGet<JsonRecord>("/chaos/executor/safety/policy"), ...poll });
}

export function useLiveDemoStatus() {
  return useQuery({ queryKey: ["live-demo-status"], queryFn: () => apiGet<LiveDemoStatus>("/live-demo/status"), ...poll });
}

export function useCreateChaosPlan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/chaos/planner/plans", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chaos-plans"] }),
  });
}

export function useDryRunChaos() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/chaos/executor/runs", { ...body, dry_run: true, approved: false }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chaos-runs"] }),
  });
}

export function useCreateChaosCampaign() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<{ campaign: ChaosCampaign }>("/chaos/planner/campaigns", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chaos-campaigns"] }),
  });
}

export function useStartChaosCampaign() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ campaignId, ...body }: { campaignId: string } & JsonRecord) => apiPost<{ run: ChaosCampaignRun; report: JsonRecord; steps: JsonRecord[] }>(`/chaos/planner/campaigns/${campaignId}/start`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["chaos-campaigns"] });
      qc.invalidateQueries({ queryKey: ["chaos-campaign-runs"] });
    },
  });
}

export function useControlChaosCampaign(action: "pause" | "resume" | "stop") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (campaignId: string) => apiPost<{ campaign: ChaosCampaign }>(`/chaos/planner/campaigns/${campaignId}/${action}`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chaos-campaigns"] }),
  });
}

export function useExecuteChaos() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/chaos/executor/runs", { ...body, dry_run: false, approved: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["chaos-runs"] }),
  });
}

export function useRemediationPlans(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["remediation-plans", filters], queryFn: () => apiGet<{ plans: RemediationPlan[]; count: number }>("/remediation/recommender/plans", filters), ...poll });
}

export function useApprovals(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["approvals", filters], queryFn: () => apiGet<{ approvals: Approval[]; count: number }>("/remediation/approval/approvals", filters), ...poll });
}

export function useExecutions(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["executions", filters], queryFn: () => apiGet<{ executions: RemediationExecution[]; count: number }>("/remediation/executor/executions", filters), ...poll });
}

export function useRemediationVerifications(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["remediation-verifications", filters], queryFn: () => apiGet<{ verifications: RemediationVerification[]; count: number }>("/remediation/executor/verifications", filters), ...poll });
}

export function useRemediationRollbackPlans(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["remediation-rollback-plans", filters], queryFn: () => apiGet<{ rollback_plans: RemediationRollbackPlan[]; count: number }>("/remediation/executor/rollback-plans", filters), ...poll });
}

export function useRemediationPolicy() {
  return useQuery({ queryKey: ["remediation-policy"], queryFn: () => apiGet<JsonRecord>("/remediation/executor/safety/policy"), ...poll });
}

export function useCreateRemediationPlan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/remediation/recommender/plans", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["remediation-plans"] }),
  });
}

export function useCreateApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/remediation/approval/approvals", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
}

export function useApprovalStatus(planId?: string) {
  return useQuery({
    queryKey: ["approval-status", planId],
    enabled: Boolean(planId),
    queryFn: () => apiGet<JsonRecord>(`/remediation/approval/plans/${planId}/approval-status`),
    ...poll,
  });
}

export function useDryRunRemediation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/remediation/executor/executions/dry-run", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["executions"] }),
  });
}

export function useExecuteRemediation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<JsonRecord>("/remediation/executor/executions", { ...body, dry_run: false }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["executions"] }),
  });
}

export function useAutopilotMode() {
  return useQuery({ queryKey: ["autopilot-mode"], queryFn: () => apiGet<JsonRecord>("/autopilot/mode"), ...poll });
}

export function useAutopilotRuns(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["autopilot-runs", filters], queryFn: () => apiGet<{ runs: AutopilotRun[]; count: number }>("/autopilot/runs", filters), ...poll });
}

export function useAutopilotRun(id?: string) {
  return useQuery({ queryKey: ["autopilot-run", id], enabled: Boolean(id), queryFn: () => apiGet<{ run: AutopilotRun; steps: AutopilotStep[] }>(`/autopilot/runs/${id}`), ...poll });
}

export function useCreateAutopilotRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<{ run: AutopilotRun }>("/autopilot/runs", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["autopilot-runs"] }),
  });
}

export function useSchedulerStatus() {
  return useQuery({ queryKey: ["scheduler-status"], queryFn: () => apiGet<SchedulerStatus>("/scheduler/scheduler/status"), ...poll });
}

export function useSchedulerItems() {
  return useQuery({ queryKey: ["scheduler-items"], queryFn: () => apiGet<{ items: SchedulerItem[]; count: number }>("/scheduler/scheduler/items"), ...poll });
}

export function useSchedulerHistory(filters: JsonRecord = {}) {
  return useQuery({ queryKey: ["scheduler-history", filters], queryFn: () => apiGet<{ decisions: SchedulerDecision[]; count: number }>("/scheduler/scheduler/history", filters), ...poll });
}

export function useControlSchedulerItem(action: "enable" | "disable" | "pause" | "resume" | "run") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (itemId: string) => apiPost<JsonRecord>(`/scheduler/scheduler/items/${itemId}/${action}`, {}),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["scheduler-status"] });
      qc.invalidateQueries({ queryKey: ["scheduler-items"] });
      qc.invalidateQueries({ queryKey: ["scheduler-history"] });
    },
  });
}

export function useCreateSchedulerItem() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: JsonRecord) => apiPost<{ item: SchedulerItem }>("/scheduler/scheduler/items", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["scheduler-status"] });
      qc.invalidateQueries({ queryKey: ["scheduler-items"] });
    },
  });
}

const healthTargets = [
  ["retrieval-service", "/retrieval/health"],
  ["knowledge-retrieval-service", "/knowledge/health"],
  ["topology-service", "/topology/health"],
  ["causal-reconstruction-service", "/causality/health"],
  ["incident-timeline-service", "/timeline/health"],
  ["agent-tool-gateway", "/tools/health"],
  ["agent-orchestrator-service", "/agent/health"],
  ["autopilot-service", "/autopilot/health"],
  ["scheduler-service", "/scheduler/health"],
  ["chaos-planner-service", "/chaos/planner/health"],
  ["chaos-executor-service", "/chaos/executor/health"],
  ["remediation-recommender-service", "/remediation/recommender/health"],
  ["approval-service", "/remediation/approval/health"],
  ["remediation-executor-service", "/remediation/executor/health"],
  ["feature-extractor-service", "/features/health"],
  ["anomaly-detector-service", "/anomaly/health"],
] as const;

export function useSystemHealth() {
  return useQuery({
    queryKey: ["system-health"],
    queryFn: async (): Promise<HealthCheck[]> => Promise.all(healthTargets.map(checkHealth)),
    ...poll,
  });
}

export function useQaProjects(limit = 50) {
  return useQuery({
    queryKey: ["qa-projects", limit],
    queryFn: () => apiGet<{ projects: QaProjectSummary[]; count: number }>("/qa/projects", { limit }),
    ...poll,
  });
}

export function useEvalFixtures() {
  return useQuery({
    queryKey: ["qa-eval-fixtures"],
    queryFn: () => apiGet<{ fixtures: QaEvalFixture[]; count: number }>("/qa/evals"),
    ...poll,
  });
}

export function useProjectEvaluations(projectId?: string, limit = 20) {
  return useQuery({
    queryKey: ["qa-project-evaluations", projectId, limit],
    enabled: Boolean(projectId),
    queryFn: () => apiGet<{ project_id: string; evaluations: QaEvaluation[]; count: number }>(`/qa/projects/${projectId}/evaluations`, { limit }),
    ...poll,
  });
}

async function checkHealth([name, path]: readonly [string, string]): Promise<HealthCheck> {
  const started = performance.now();
  try {
    const data = await apiGet<unknown>(path);
    return { name, ok: true, latencyMs: Math.round(performance.now() - started), data };
  } catch (error) {
    return { name, ok: false, latencyMs: Math.round(performance.now() - started), error: error instanceof Error ? error.message : String(error) };
  }
}
