import { FormEvent, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { Card, Empty, ErrorState, Loading, PageHead } from "../components/ui";

interface Result { text: string; source: string; chunk: number | null; score: number }

export default function Knowledge() {
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const query = useQuery({
    queryKey: ["kb", term],
    queryFn: () => api.get<{ results: Result[] }>(`/api/kb/search?q=${encodeURIComponent(term)}`),
    enabled: term.length >= 3,
  });
  const submit = (e: FormEvent) => { e.preventDefault(); setTerm(input.trim()); };
  return (
    <>
      <PageHead eyebrow="Knowledge base" title="Harrison's Principles of Internal Medicine" subtitle="The passages CDS grounds its recommendations in (local, offline search)" />
      <Card>
        <form className="row" onSubmit={submit}>
          <input className="input" style={{ flex: 1 }} placeholder="e.g. diabetic neuropathy foot care, statin therapy in diabetes" value={input} onChange={(e) => setInput(e.target.value)} />
          <button className="btn primary" disabled={input.trim().length < 3}>Search</button>
        </form>
      </Card>
      {query.isFetching && <Loading label="Searching…" />}
      {query.isError && <ErrorState error={query.error} onRetry={() => void query.refetch()} />}
      {query.data && (
        <Card title={`${query.data.results.length} passage(s)`} className="mt">
          {query.data.results.length ? query.data.results.map((r, i) => (
            <div key={i} className="excerpt" style={{ marginBottom: 8 }}>
              <small>Harrison's · passage {r.chunk ?? "?"} · relevance {Math.round(r.score * 100)}%</small>{r.text}
            </div>
          )) : <Empty>No passage matched. Try different words.</Empty>}
        </Card>
      )}
    </>
  );
}
