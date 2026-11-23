import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import type { DashboardResponse } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { RecordTable } from "../components/RecordTable";
import { Banner, Card, Empty, ErrorState, Kpi, Loading, PageHead } from "../components/ui";

const WINDOWS = ["All Time", "Today", "Last 7 Days", "Last 30 Days"];
const STATUSES = ["All Statuses", "Pending", "Approved"];

export default function Dashboard() {
  const navigate = useNavigate();
  const { can } = useAuth();
  const [q, setQ] = useState("");
  const [window, setWindow] = useState(WINDOWS[0]);
  const [status, setStatus] = useState(STATUSES[0]);
  const [mine, setMine] = useState(false);
  const params = new URLSearchParams({ q, window, status, mine: String(mine) });
  const query = useQuery({
    queryKey: ["dashboard", q, window, status, mine],
    queryFn: () => api.get<DashboardResponse>(`/api/dashboard?${params}`),
    placeholderData: (previous) => previous,
  });
  const data = query.data;

  return (
    <>
      <PageHead eyebrow="Clinical overview" title="Clinical Operations" subtitle="Consultation, documentation and coding throughput">
        {can("Clinical Review", "Edit") && <button className="btn primary" onClick={() => navigate("/consultations/new")}>+ New consultation</button>}
      </PageHead>
      <div className="row" style={{ marginBottom: 14 }}>
        <input className="input" style={{ maxWidth: 320 }} placeholder="Search name / MRN / consultation ID" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
        <select className="input" style={{ width: "auto" }} value={window} onChange={(e) => setWindow(e.target.value)} aria-label="Date window">
          {WINDOWS.map((w) => <option key={w}>{w}</option>)}
        </select>
        <select className="input" style={{ width: "auto" }} value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          {STATUSES.map((s) => <option key={s}>{s}</option>)}
        </select>
        <label className="checkbox"><input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} /> My work only</label>
      </div>
      {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
      {!data && query.isLoading && <Loading />}
      {data && (
        <>
          <div className="grid grid-5" style={{ marginBottom: 14 }}>
            <Kpi tone="lav" label="Total consultations" value={data.kpis.total} hint="All recorded consults" />
            <Kpi tone="sky" label="Today's volume" value={data.kpis.today} hint="Created today" />
            <Kpi tone="butter" label="Pending sign-off" value={data.kpis.pending} hint="SOAP drafts awaiting review" />
            <Kpi tone="mint" label="Signed notes" value={data.kpis.signed} hint={`${data.kpis.sign_off_rate}% sign-off rate`} />
            <Kpi tone="peach" label="Coded encounters" value={data.kpis.coded} hint="ICD-10 / CPT suggestions saved" />
          </div>
          {!data.ehr.connected && <Banner tone="warn" title="EHR unavailable">{data.ehr.error}</Banner>}
          <div className="split">
            <Card title="Consultation activity" subtitle="Daily consultations and sign-offs">
              {data.activity.length ? (
                <div style={{ height: 240 }}>
                  <ResponsiveContainer>
                    <LineChart data={data.activity} margin={{ left: -18, right: 8, top: 8 }}>
                      <CartesianGrid stroke="#e8e9f2" vertical={false} />
                      <XAxis dataKey="date" tick={{ fontSize: 12, fill: "#6b7186" }} tickFormatter={(d: string) => d.slice(5)} />
                      <YAxis allowDecimals={false} tick={{ fontSize: 12, fill: "#6b7186" }} />
                      <Tooltip />
                      <Legend />
                      <Line type="monotone" dataKey="consults" name="New consults" stroke="#2a9d74" strokeWidth={3} dot={{ r: 3 }} />
                      <Line type="monotone" dataKey="signed" name="Signed" stroke="#6b5bd6" strokeWidth={3} dot={{ r: 3 }} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : <Empty>No activity for these filters.</Empty>}
            </Card>
            <Card title="Operating position" subtitle="Documentation backlog">
              <div className="grid grid-3">
                <Kpi tone="butter" label="Pending" value={data.kpis.pending} />
                <Kpi tone="mint" label="Sign-off" value={`${data.kpis.sign_off_rate}%`} />
                <Kpi tone="sky" label="Today" value={data.kpis.today} />
              </div>
              {data.ehr.connected && <p className="muted small" style={{ marginBottom: 0 }}>EHR connected · {data.ehr.patients} patients</p>}
            </Card>
          </div>
          <Card title="Patient records" subtitle="Clinical documentation queue" className="mt">
            <RecordTable rows={data.queue} onOpen={(row) => navigate(`/consultations/${row.id}`)} empty="No consultations match these filters." />
          </Card>
        </>
      )}
    </>
  );
}
