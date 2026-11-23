import type { ReactNode } from "react";
import type { RecordSummary } from "../api/types";
import { Badge, Empty, STATUS_TONE } from "./ui";

export function RecordTable({ rows, onOpen, empty = "No consultations yet.", showStage = true, selectedId, actions }: {
  rows: RecordSummary[]; onOpen: (row: RecordSummary) => void; empty?: string; showStage?: boolean; selectedId?: string;
  /** Optional per-row buttons (clicks inside don't open the row). */
  actions?: (row: RecordSummary) => ReactNode;
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
            {actions && <th><span className="sr-only">Actions</span></th>}
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
              {actions && <td onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>{actions(row)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
