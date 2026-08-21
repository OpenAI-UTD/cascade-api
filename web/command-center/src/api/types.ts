export type JsonRecord = Record<string, unknown>;

export type CountMap = Record<string, number | null>;

export type HealthState = "healthy" | "degraded" | "unhealthy" | "unknown" | "unavailable" | "insufficient_evidence" | "not_applicable";
export type RiskLevel = "low" | "medium" | "high" | "critical" | "unknown" | "unavailable" | "insufficient_evidence" | "not_applicable";
export type PolicyMode = "read_only" | "dry_run" | "local_demo" | "production_safe";
export type AutonomyLevel = 0 | 1 | 2 | 3 | 4 | 5;

export interface ContractMeta extends JsonRecord {
  schema_version?: string;
  contract_schema_version?: string;
  correlation_id?: string;
  run_id?: string;
}

export interface ServiceIdentity extends ContractMeta {
  service?: string;
  namespace?: string;
  workload?: string;
  cluster?: string;
  environment?: string;
  labels?: Record<string, string>;
}

export interface TargetRef extends ContractMeta {
  service?: string;
  namespace?: string;
  workload?: string;
  resource_kind?: string;
  resource_name?: string;
  selector?: JsonRecord;
}

export interface EvidenceBundle extends ContractMeta {
  bundle_id?: string;
  created_at?: string;
  target?: TargetRef;
  quality?: string;
  summary?: string;
  refs?: JsonRecord[];
  metric_windows?: MetricWindow[];
  limitations?: string[];
}

export interface Recommendation extends ContractMeta {
  recommendation_id?: string;
  created_at?: string;
  rca_report_id?: string;
  anomaly_id?: string;
  target?: TargetRef;
  action_type?: string;
  summary?: string;
  confidence?: number;
  risk_level?: RiskLevel | string;
  evidence_bundle_id?: string;
  evidence_refs?: JsonRecord[];
}

export interface ApiErrorBody {
  detail?: unknown;
  message?: string;
}

export interface TelemetryEvent extends JsonRecord {
  event_id?: string;
  timestamp?: string;
  service?: string;
  namespace?: string;
  event_type?: string;
  metric_name?: string;
  metric_value?: number;
  severity?: string;
}

export interface FeatureWindow extends JsonRecord {
  window_id?: string;
  window_start?: string;
  service?: string;
  namespace?: string;
  event_count?: number;
  error_count?: number;
  latency_p95_ms?: number;
  latency_p99_ms?: number;
  request_rate?: number;
  error_rate?: number;
  readiness_rate?: number;
  availability_rate?: number;
  warning_event_count?: number;
  missing_metric_count?: number;
}

export interface MetricWindow extends ContractMeta {
  window_id?: string;
  service?: string;
  namespace?: string;
  window_start?: string;
  window_end?: string;
  event_count?: number;
  request_rate?: number | null;
  error_rate?: number | null;
  latency_p95_ms?: number | null;
  latency_p99_ms?: number | null;
  readiness_rate?: number | null;
  availability_rate?: number | null;
  health_state?: HealthState;
  evidence_quality?: string;
  metrics?: Record<string, number | null>;
}

export interface Anomaly extends ContractMeta {
  anomaly_id?: string;
  detected_at?: string;
  target?: TargetRef;
  service?: string;
  namespace?: string;
  severity?: string;
  risk_score?: number;
  detector_type?: string;
  model_name?: string;
  explanation?: string;
  evidence?: EvidenceBundle | JsonRecord;
}

export interface Incident extends JsonRecord {
  incident_id?: string;
  started_at?: string;
  service?: string;
  affected_services?: unknown;
  severity?: string;
  title?: string;
  summary?: string;
}

export interface KnowledgeResult extends JsonRecord {
  score?: number;
  chunk_id?: string;
  title?: string;
  source_type?: string;
  source_path?: string;
  source_uri?: string;
  service?: string;
  phase?: string;
  severity?: string;
  chunk_text?: string;
  metadata?: unknown;
}

export interface Investigation extends JsonRecord {
  investigation_id?: string;
  created_at?: string;
  status?: string;
  service?: string;
  namespace?: string;
  trigger_type?: string;
  title?: string;
  objective?: string;
  final_summary?: string;
  confidence?: number;
  tools_used?: string[];
  evidence_refs?: unknown;
}

export interface ExperimentEvent extends JsonRecord {
  experiment_id?: string;
  event_id?: string;
  timestamp?: string;
  started_at?: string;
  completed_at?: string;
  service?: string;
  target_service?: string;
  event_type?: string;
  status?: string;
}

export interface TargetWorkload extends JsonRecord {
  name?: string;
  namespace?: string;
  frontend_service?: string;
  services?: string[];
  dependencies?: Record<string, string[]>;
  dependency_edges?: Array<[string, string]>;
  safe_chaos_services?: string[];
  protected_services?: string[];
}

export interface TopologyGraph extends ContractMeta {
  snapshot_id?: string;
  captured_at?: string;
  discovery_status?: string;
  source_summary?: JsonRecord;
  traffic_summary?: JsonRecord;
  metric_availability?: JsonRecord;
  red_metric_availability?: JsonRecord;
  warnings?: string[];
  limitations?: string[];
  dependencies?: Record<string, string[]>;
  topology?: Record<string, string[]>;
  nodes?: Array<JsonRecord & { id?: string; name?: string; service?: string; kind?: string; namespace?: string; source_type?: string; confidence?: number; evidence?: string; health_status?: string }>;
  edges?: Array<JsonRecord & { id?: string; source?: string; target?: string; from?: string; to?: string; relation?: string; source_type?: string; confidence?: number; evidence?: string }>;
}

export interface BlastRadius extends JsonRecord {
  root_service?: string;
  service_name?: string;
  impact_path?: string[];
  affected_services?: string[];
  upstream?: string[];
  downstream?: string[];
  score?: number;
  blast_radius_score?: number;
}

export interface CausalIncident extends JsonRecord {
  incident_id?: string;
  experiment_id?: string;
  root_cause_service?: string;
  causal_chain?: string[];
  affected_services?: string[];
  confidence?: string | number;
  evidence?: unknown;
  started_at?: string;
  generated_at?: string;
  summary?: string;
}

export interface CausalReport extends JsonRecord {
  report_id?: string;
  investigation_id?: string;
  incident_id?: string;
  title?: string;
  summary?: string;
  suspected_root_cause?: string;
  causal_summary?: string;
  affected_services?: unknown;
  evidence?: unknown;
  rejected_alternatives?: unknown;
  recommended_next_steps?: unknown;
  suggested_remediation?: unknown;
  limitations?: unknown;
  markdown_report?: string;
  confidence?: number;
}

export interface RcaReport extends ContractMeta {
  report_id?: string;
  generated_at?: string;
  status?: string;
  target?: TargetRef;
  target_service?: string;
  likely_root_cause_service?: string;
  affected_downstream_services?: string[];
  confidence_score?: number;
  related_chaos_experiment?: JsonRecord | null;
  evidence?: JsonRecord[];
  timeline?: JsonRecord[];
  limitations?: string[];
  explanation?: string;
  recommendation_id?: string;
  topology_snapshot_id?: string;
}

export interface InvestigationEvidence extends JsonRecord {
  hypothesis?: string;
  evidence?: unknown;
  evidence_refs?: unknown;
  rejected_alternatives?: unknown;
  causal_summary?: string;
  blast_radius_summary?: string;
  recommended_safe_action?: string;
}

export interface PolicyAuditRecord extends JsonRecord {
  audit_id?: string;
  checked_at?: string;
  plan_id?: string;
  action_type?: string;
  namespace?: string;
  service?: string;
  allowed?: boolean | number;
  risk_level?: string;
  findings?: unknown;
  policy?: unknown;
}

export interface ChaosPlan extends JsonRecord {
  plan_id?: string;
  created_at?: string;
  status?: string;
  experiment_kind?: string;
  target_service?: string;
  target_namespace?: string;
  risk_level?: string;
  safety_score?: number;
  objective?: string;
  safety_findings?: unknown;
  safety_policy?: unknown;
  protected_services?: unknown;
  denied_resource_kinds?: unknown;
  blast_radius_score?: number;
  hypothesis?: string;
  expected_impact?: string;
  plan?: JsonRecord;
  manifest?: JsonRecord;
}

export interface ChaosRun extends JsonRecord {
  run_id?: string;
  plan_id?: string;
  started_at?: string;
  status?: string;
  dry_run?: boolean;
  experiment_kind?: string;
  target_service?: string;
  target_namespace?: string;
  manifest?: JsonRecord;
  safety?: JsonRecord;
  cleanup_status?: string;
}

export interface ChaosExperiment extends ContractMeta {
  experiment_id?: string;
  plan_id?: string;
  experiment_kind?: string;
  target?: TargetRef;
  status?: string;
  dry_run?: boolean;
  approved?: boolean;
  started_at?: string | null;
  completed_at?: string | null;
  evidence_bundle_id?: string;
  rca_report_id?: string;
  verification_id?: string;
}

export interface ResilienceScore extends JsonRecord {
  score_id?: string;
  computed_at?: string;
  service?: string;
  namespace?: string;
  experiment_kind?: string;
  resilience_score?: number;
  grade?: string;
  explanation?: string;
}

export interface ChaosCampaign extends ContractMeta {
  campaign_id?: string;
  created_at?: string;
  updated_at?: string;
  status?: string;
  name?: string;
  target_namespace?: string;
  allowed_services?: string[];
  experiment_templates?: JsonRecord[];
  schedule?: JsonRecord;
  max_experiments_per_run?: number;
  blast_radius_limit?: number;
  cooldown_seconds?: number;
  dry_run?: boolean;
  local_demo_execution_enabled?: boolean;
  next_run_at?: string;
}

export interface ChaosCampaignRun extends JsonRecord {
  run_id?: string;
  campaign_id?: string;
  started_at?: string;
  completed_at?: string | null;
  status?: string;
  dry_run?: boolean;
  experiments_attempted?: number;
  experiments_succeeded?: number;
  experiments_blocked?: number;
  experiments_failed?: number;
  services?: string[];
  report?: JsonRecord;
  error_message?: string;
}

export interface PolicyDecision extends ContractMeta {
  policy_decision_id?: string;
  decided_at?: string;
  recommendation_id?: string;
  allowed?: boolean;
  status?: string;
  action_type?: string;
  mode?: PolicyMode | string;
  autonomy_level?: AutonomyLevel | number;
  required_autonomy_level?: AutonomyLevel | number;
  risk_level?: RiskLevel | string;
  risk_score?: number;
  blast_radius?: number;
  requires_approval?: boolean;
  dry_run_only?: boolean;
  allowed_automatically?: boolean;
  rollback_available?: boolean;
  reasons?: string[];
}

export interface RemediationPlan extends ContractMeta {
  plan_id?: string;
  created_at?: string;
  status?: string;
  recommendation_id?: string;
  rca_report_id?: string;
  target?: TargetRef;
  trigger_type?: string;
  service?: string;
  namespace?: string;
  confidence?: number;
  action_type?: string;
  action_summary?: string;
  remediation_steps?: unknown;
  rollback_steps?: unknown;
  post_checks?: unknown;
  evidence_refs?: unknown;
  safety_findings?: unknown;
  dry_run_manifest?: JsonRecord;
  policy_decision?: PolicyDecision | JsonRecord;
  denied_resource_kinds?: unknown;
  protected_services?: unknown;
}

export interface Approval extends JsonRecord {
  approval_id?: string;
  plan_id?: string;
  decided_at?: string;
  decision?: string;
  approver?: string;
  approver_role?: string;
  reason?: string;
}

export interface RemediationExecution extends ContractMeta {
  execution_id?: string;
  plan_id?: string;
  started_at?: string;
  status?: string;
  dry_run?: boolean;
  executed?: boolean;
  validation_status?: string;
  action_type?: string;
  target?: TargetRef;
  policy_decision_id?: string;
  output_summary?: string;
  rollback_available?: boolean;
  execution_status?: string;
  output?: unknown;
  safety?: JsonRecord;
}

export interface RemediationVerification extends ContractMeta {
  verification_id?: string;
  execution_id?: string;
  plan_id?: string;
  rollback_plan_id?: string;
  completed_at?: string;
  action_type?: string;
  namespace?: string;
  service?: string;
  status?: string;
  evidence_quality?: string;
  rollback_status?: string;
  summary?: string;
  before?: JsonRecord;
  after?: JsonRecord;
  comparisons?: unknown;
  limitations?: unknown;
}

export interface RemediationRollbackPlan extends ContractMeta {
  rollback_plan_id?: string;
  execution_id?: string;
  plan_id?: string;
  created_at?: string;
  action_type?: string;
  namespace?: string;
  service?: string;
  rollback_type?: string;
  available?: boolean;
  auto_executable?: boolean;
  status?: string;
  reason?: string;
  actions?: unknown;
  result?: unknown;
}

export interface AutopilotRun extends ContractMeta {
  run_id?: string;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
  status?: string;
  final_result?: string;
  mode?: string;
  trigger_type?: string;
  trigger_id?: string;
  service?: string;
  namespace?: string;
  target?: TargetRef;
  objective?: string;
  anomaly_id?: string;
  rca_report_id?: string;
  recommendation_id?: string;
  policy_decision_id?: string;
  investigation_id?: string;
  remediation_plan_id?: string;
  approval_id?: string;
  dry_run_execution_id?: string;
  execution_id?: string;
  verification_id?: string;
  rollback_plan_id?: string;
  chaos_experiment_id?: string;
  topology_snapshot_id?: string;
  evidence_bundle_id?: string;
  proposed_action?: string;
  error_message?: string;
  evidence?: JsonRecord;
  recommendation?: JsonRecord;
  action?: JsonRecord;
  verification?: JsonRecord;
}

export interface AutopilotStep extends ContractMeta {
  step_id?: string;
  run_id?: string;
  created_at?: string;
  state?: string;
  status?: string;
  summary?: string;
  input?: JsonRecord;
  output?: JsonRecord;
  error_message?: string;
}

export interface SchedulerItem extends JsonRecord {
  item_id?: string;
  item_type?: "chaos_campaign" | "autopilot_run" | string;
  target_id?: string;
  name?: string;
  schedule?: JsonRecord;
  enabled?: boolean;
  paused?: boolean;
  mode?: string;
  next_run_at?: string | null;
  last_run_at?: string | null;
  cooldown_seconds?: number;
  max_runs_per_window?: number;
  window_seconds?: number;
  failure_count?: number;
  status?: string;
  payload?: JsonRecord;
}

export interface SchedulerDecision extends JsonRecord {
  decision_id?: string;
  item_id?: string;
  item_type?: string;
  target_id?: string;
  evaluated_at?: string;
  due_at?: string | null;
  status?: string;
  action?: string;
  reason?: string;
  idempotency_key?: string;
  run_id?: string;
  dry_run?: boolean;
  skipped_missed_windows?: number;
  payload?: JsonRecord;
  error_message?: string;
}

export interface SchedulerStatus extends JsonRecord {
  service?: string;
  enabled?: boolean;
  mode?: string;
  poll_interval_seconds?: number;
  items?: number;
  due?: number;
  last_decision?: SchedulerDecision | null;
}

export interface LivePolicyStatus extends JsonRecord {
  available?: boolean;
  live_demo_mode?: boolean;
  dangerous_actions_enabled?: boolean;
  real_chaos_enabled?: boolean;
  real_remediation_enabled?: boolean;
  execution_enabled?: boolean;
  allowed_target_namespace?: string;
  allowed_namespaces?: string[];
  allowed_services?: string[];
  protected_services?: string[];
  denied_services?: string[];
  allowed_actions?: string[];
  disabled_reasons?: string[];
}

export interface LiveDemoStatus extends JsonRecord {
  command_center?: { dangerous_actions_enabled?: boolean };
  chaos?: LivePolicyStatus;
  remediation?: LivePolicyStatus;
  target?: TargetWorkload;
}

export interface HealthCheck {
  name: string;
  ok: boolean;
  latencyMs: number;
  data?: unknown;
  error?: string;
}

export interface QaProjectSummary {
  project_id: string;
  name?: string;
  team?: string;
  visibility?: string;
  quota_runs_per_day?: number;
  quota_remaining?: number;
  registered_at?: string;
  repository?: string;
  last_run_at?: string;
  run_count?: number;
  gate_passed?: boolean | null;
  findings?: number;
}

export interface QaEvalFixture {
  fixture_id: string;
  title: string;
  category: string;
  expect_status?: string;
}

export interface QaEvaluation extends JsonRecord {
  evaluation_id?: string;
  created_at?: string;
  status?: string;
  summary?: string;
  quality_gate?: { passed?: boolean; blocking_findings?: number; fail_on?: string; reason?: string };
  findings?: JsonRecord[];
}

export interface AuditEvent extends ContractMeta {
  event_id?: string;
  timestamp?: string;
  event_type?: string;
  subsystem?: string;
  severity?: string;
  run_id?: string;
  correlation_id?: string;
  service?: string;
  namespace?: string;
  actor?: string;
  action?: string;
  decision?: string;
  status?: string;
  risk_level?: string;
  policy_decision_id?: string;
  recommendation_id?: string;
  remediation_execution_id?: string;
  verification_id?: string;
  rollback_plan_id?: string;
  autopilot_run_id?: string;
  chaos_experiment_id?: string;
  campaign_id?: string;
  rca_report_id?: string;
  topology_snapshot_id?: string;
  evidence_bundle_id?: string;
  topology_node_or_edge_id?: string;
  evidence_summary?: string;
  raw_payload_json?: string;
  user_safe_message?: string;
}
