import type { RecordSummary } from "../api/types";
import { Badge, Empty, STATUS_TONE } from "./ui";

export function RecordTable({ rows, onOpen, empty = "No consultations yet.", showStage = true, selectedId }: {
  rows: RecordSummary[]; onOpen: (row: RecordSummary) => void; empty?: string; showStage?: boolean; selectedId?: string;
}) {
  if (!rows.length) return <Empty>{empty}</Empty>;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Patient</th>
            <th>MRN</th>
            <th>Consultation</th>
            <th>Date</th>
            <th>Clinician</th>
            <th>Note</th>
            {showStage && <th>Stage</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className={`clickable ${row.id === selectedId ? "selected" : ""}`} onClick={() => onOpen(row)}
              tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onOpen(row)}>
              <td><b>{row.name || "Unknown patient"}</b></td>
              <td className="mono">{row.mrn || "—"}</td>
              <td className="mono">{row.id}</td>
              <td>{row.date} <span className="muted small">{row.time}</span></td>
              <td>{row.doctor || "—"}</td>
              <td><Badge tone={STATUS_TONE[row.status] ?? "gray"}>{row.status === "Approved" ? "Signed" : "Draft"}</Badge></td>
              {showStage && <td><Badge tone={STATUS_TONE[row.stage] ?? "gray"}>{row.stage}</Badge></td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
