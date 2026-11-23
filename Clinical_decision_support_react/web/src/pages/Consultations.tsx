import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { RecordSummary } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { RecordTable } from "../components/RecordTable";
import { Card, ErrorState, Loading, PageHead, Tabs } from "../components/ui";

type Filter = "all" | "drafts" | "signed";

export default function Consultations() {
  const navigate = useNavigate();
  const { can } = useAuth();
  const [filter, setFilter] = useState<Filter>("drafts");
  const [q, setQ] = useState("");
  const query = useQuery({ queryKey: ["consultations"], queryFn: () => api.get<{ consultations: RecordSummary[] }>("/api/consultations") });
  const rows = useMemo(() => {
    const term = q.trim().toLowerCase();
    return (query.data?.consultations ?? []).filter((row) => {
      if (filter === "drafts" && row.status === "Approved") return false;
      if (filter === "signed" && row.status !== "Approved") return false;
      return !term || `${row.name} ${row.mrn} ${row.id}`.toLowerCase().includes(term);
    });
  }, [query.data, filter, q]);

  return (
    <>
      <PageHead eyebrow="Consultations" title="Clinical Review & CDS" subtitle="Open a consultation to review its SOAP draft, decision support and sign it.">
        {can("Clinical Review", "Edit") && <button className="btn primary" onClick={() => navigate("/consultations/new")}>+ New consultation</button>}
      </PageHead>
      <Card>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <Tabs<Filter> value={filter} onChange={setFilter} tabs={[{ id: "drafts", label: "Awaiting review" }, { id: "signed", label: "Signed" }, { id: "all", label: "All" }]} />
          <input className="input" style={{ maxWidth: 280 }} placeholder="Search" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search consultations" />
        </div>
        {query.isLoading && <Loading />}
        {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
        {query.data && <RecordTable rows={rows} onOpen={(row) => navigate(`/consultations/${row.id}`)} empty={filter === "drafts" ? "Nothing awaiting review." : "No consultations."} />}
      </Card>
    </>
  );
}
