import { NavLink } from "react-router-dom";
import { AlertTriangle, Beaker, Bell, BookOpen, Bot, CalendarClock, ChevronDown, FileSearch, FlaskConical, Gauge, GitBranch, History, Home, Info, Network, Plus, RadioTower, RefreshCw, Shield, Stethoscope, Wrench, BadgeCheck } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { dangerousActionsEnabled } from "../../api/client";
import { useSystemHealth } from "../../api/hooks";

const nav = [
  { to: "/", label: "Overview", icon: Home },
  { to: "/quality", label: "Quality", icon: BadgeCheck },
  { to: "/evals", label: "Evals", icon: FlaskConical },
  { to: "/telemetry", label: "Telemetry", icon: RadioTower },
  { to: "/anomalies", label: "Anomalies", icon: AlertTriangle },
  { to: "/audit", label: "Audit", icon: History },
  { to: "/autopilot", label: "Autopilot", icon: Bot },
  { to: "/investigations", label: "Investigations", icon: Stethoscope },
  { to: "/topology", label: "Topology", icon: Network },
  { to: "/causality", label: "Causality", icon: GitBranch },
  { to: "/knowledge", label: "Knowledge", icon: BookOpen },
  { to: "/chaos", label: "Chaos", icon: Beaker },
  { to: "/remediation", label: "Remediation", icon: Wrench },
  { to: "/scheduler", label: "Scheduler", icon: CalendarClock },
  { to: "/incidents", label: "Incidents", icon: FileSearch },
  { to: "/system", label: "System", icon: Gauge },
  { to: "/about", label: "About", icon: Info },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const health = useSystemHealth();
  const queryClient = useQueryClient();
  const degraded = health.data?.filter((item) => !item.ok).length ?? 0;
  const liveMode = readDangerousFlag(health.data) ?? dangerousActionsEnabled;
  const updated = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
            <path d="M25.7 9.5a11 11 0 1 0 0 13" />
            <path d="M15.6 5.2a11 11 0 0 1 9.3 5.1l-6 3.5a4.1 4.1 0 1 0 0 4.5l6 3.4a11 11 0 0 1-9.3 5.1" />
          </svg>
          <div>
            <strong>Cascade</strong>
            <span>Club Quality + Reliability Studio</span>
          </div>
        </div>
        <div className={`safe-badge ${liveMode ? "live" : ""}`}>
          <Shield size={14} />
          {liveMode ? "LIVE MODE" : "DRY-RUN SAFE"}
        </div>
        <nav className="nav">
          {nav.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`} end={item.to === "/"}>
                {({ isActive }) => (
                  <>
                    <Icon size={20} aria-hidden="true" />
                    <span>{item.label}</span>
                    {isActive ? <span className="sr-only" aria-current="page">current page</span> : null}
                  </>
                )}
              </NavLink>
            );
          })}
        </nav>
        <div className="service-mini">
          <span className={`service-dot ${degraded ? "warn" : "ok"}`} />
          <span>{degraded ? `${degraded} degraded` : "Services OK"}</span>
        </div>
        <div className="sidebar-tools">
          <a href="/system"><Gauge size={18} /> Settings</a>
          <a href="/about"><Info size={18} /> Integrations</a>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <div>
            <div className="eyebrow">cascade-system</div>
            <h1>Operator Command Center</h1>
          </div>
          <div className="topbar-actions">
            <button type="button" className="ghost-button" aria-label="Refresh dashboard data" onClick={() => void queryClient.invalidateQueries()}>
              <RefreshCw size={16} /> Refresh <ChevronDown size={14} />
            </button>
            <a className="primary-action" href="/investigations"><Plus size={18} /> Start Investigation</a>
            <span className="topbar-divider" />
            <button type="button" className="icon-button" aria-label="Notifications"><Bell size={17} /></button>
          </div>
        </header>
        {children}
        <footer className="status-footer">
          <span><span className={`service-dot ${degraded ? "warn" : "ok"}`} />{degraded ? `${degraded} services degraded` : "All Systems Operational"}</span>
          <span>Data as of {updated}</span>
        </footer>
      </main>
    </div>
  );
}

function readDangerousFlag(items: unknown): boolean | undefined {
  if (!Array.isArray(items)) return undefined;
  for (const item of items) {
    const data = (item as { data?: unknown }).data as Record<string, unknown> | undefined;
    if (typeof data?.dangerous_actions_enabled === "boolean") return data.dangerous_actions_enabled;
  }
  return undefined;
}
