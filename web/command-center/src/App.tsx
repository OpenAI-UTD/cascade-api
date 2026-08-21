import { Route, Routes } from "react-router-dom";
import { Shell } from "./components/layout/Shell";
import { AboutPage } from "./pages/AboutPage";
import { AnomaliesPage } from "./pages/AnomaliesPage";
import { AuditPage } from "./pages/AuditPage";
import { AutopilotPage } from "./pages/AutopilotPage";
import { ChaosPage } from "./pages/ChaosPage";
import { CausalityPage } from "./pages/CausalityPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { InvestigationsPage } from "./pages/InvestigationsPage";
import { KnowledgePage } from "./pages/KnowledgePage";
import { OverviewPage } from "./pages/OverviewPage";
import { QualityPage } from "./pages/QualityPage";
import { EvalsPage } from "./pages/EvalsPage";
import { RemediationPage } from "./pages/RemediationPage";
import { SchedulerPage } from "./pages/SchedulerPage";
import { SystemPage } from "./pages/SystemPage";
import { TelemetryPage } from "./pages/TelemetryPage";
import { TopologyPage } from "./pages/TopologyPage";

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<OverviewPage />} />
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/quality" element={<QualityPage />} />
        <Route path="/evals" element={<EvalsPage />} />
        <Route path="/telemetry" element={<TelemetryPage />} />
        <Route path="/anomalies" element={<AnomaliesPage />} />
        <Route path="/audit" element={<AuditPage />} />
        <Route path="/autopilot" element={<AutopilotPage />} />
        <Route path="/incidents" element={<IncidentsPage />} />
        <Route path="/topology" element={<TopologyPage />} />
        <Route path="/causality" element={<CausalityPage />} />
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="/investigations" element={<InvestigationsPage />} />
        <Route path="/chaos" element={<ChaosPage />} />
        <Route path="/remediation" element={<RemediationPage />} />
        <Route path="/scheduler" element={<SchedulerPage />} />
        <Route path="/system" element={<SystemPage />} />
        <Route path="/about" element={<AboutPage />} />
      </Routes>
    </Shell>
  );
}
