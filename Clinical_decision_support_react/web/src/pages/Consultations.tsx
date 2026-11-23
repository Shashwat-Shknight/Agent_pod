import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, errorMessage } from "../api/client";
import type { RecordSummary } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { RecordTable } from "../components/RecordTable";
import { useToast } from "../components/toast";
import { Banner, Card, ErrorState, Loading, Modal, PageHead, Tabs } from "../components/ui";

type Filter = "all" | "drafts" | "signed";

export default function Consultations() {
  const navigate = useNavigate();
  const { can } = useAuth();
  const [filter, setFilter] = useState<Filter>("drafts");
  const [q, setQ] = useState("");
  const [deleting, setDeleting] = useState<RecordSummary | null>(null);
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
        {query.data && <RecordTable rows={rows} onOpen={(row) => navigate(`/consultations/${row.id}`)} empty={filter === "drafts" ? "Nothing awaiting review." : "No consultations."}
          actions={can("Clinical Review", "Edit") ? (row) => row.status !== "Approved" && (
            <button className="btn sm danger" aria-label={`Delete consultation ${row.id}`} onClick={() => setDeleting(row)}>Delete</button>
          ) : undefined} />}
      </Card>
      {deleting && <DeleteConsultation row={deleting} onClose={() => setDeleting(null)} />}
    </>
  );
}

function DeleteConsultation({ row, onClose }: { row: RecordSummary; onClose: () => void }) {
  const toast = useToast();
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.del<{ deleted: string; warnings: string[] }>(`/api/consultations/${row.id}?version=${row.version}`),
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: ["consultations"] });
      void queryClient.invalidateQueries({ queryKey: ["patients"] });
      toast(data.warnings[0] ?? `Consultation ${row.id} deleted`, data.warnings.length ? "info" : "ok");
      onClose();
    },
  });
  return (
    <Modal title="Delete this draft consultation?" onClose={onClose}>
      <p><b>{row.name}</b> · <span className="mono">{row.id}</span> · {row.date}</p>
      <p className="muted small">The draft and its scribe output are removed. The linked EHR encounter is kept and marked cancelled. Signed notes can never be deleted.</p>
      {remove.isError && <Banner tone="error" title={errorMessage(remove.error)} />}
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn" onClick={onClose}>Keep it</button>
        <button className="btn danger" disabled={remove.isPending} onClick={() => remove.mutate()}>{remove.isPending ? "Deleting…" : "Delete draft"}</button>
      </div>
    </Modal>
  );
}
