"""PDF rendering for draft bills and claims, and archiving them in the database.

PDFs are built in memory (never written to disk). ``invariant=1`` makes ReportLab output
byte-for-byte reproducible, so archiving the same document twice stores it once.
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

BRAND = colors.HexColor("#0F4C81")
MUTED = colors.HexColor("#5B6B7B")
RULE = colors.HexColor("#D5DDE5")
ZEBRA = colors.HexColor("#F4F7FA")
WARN = colors.HexColor("#8A5A00")

_styles = getSampleStyleSheet()
TITLE = ParagraphStyle("NcTitle", parent=_styles["Title"], fontSize=17, leading=21, textColor=BRAND, alignment=0, spaceAfter=2)
SUBTITLE = ParagraphStyle("NcSub", parent=_styles["Normal"], fontSize=9, textColor=MUTED, spaceAfter=8)
H2 = ParagraphStyle("NcH2", parent=_styles["Heading2"], fontSize=11.5, leading=14, textColor=BRAND, spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("NcBody", parent=_styles["Normal"], fontSize=8.8, leading=11.5)
CELL = ParagraphStyle("NcCell", parent=BODY, fontSize=8.2, leading=10.2)
CELL_R = ParagraphStyle("NcCellR", parent=CELL, alignment=TA_RIGHT)
SMALL = ParagraphStyle("NcSmall", parent=BODY, fontSize=7.6, leading=9.6, textColor=MUTED)
TOTAL = ParagraphStyle("NcTotal", parent=BODY, fontSize=11, leading=14, alignment=TA_RIGHT, textColor=BRAND)


def _t(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, (list, tuple)):
        return ", ".join(_t(item) for item in value if _t(item))
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_t(item)}" for key, item in value.items() if _t(item))
    return str(value).strip()


def _p(value: Any, style: ParagraphStyle = CELL) -> Paragraph:
    return Paragraph(escape(_t(value)).replace("\n", "<br/>") or "&nbsp;", style)


def _money(value: Any) -> str:
    try:
        return f"${float(str(value).replace('$', '').replace(',', '')):,.2f}"
    except (TypeError, ValueError):
        return _t(value) or "-"


def _key_value_table(pairs: list[tuple[str, Any]], width: float) -> Table:
    pairs = list(pairs) or [("Details", "Not recorded")]
    rows, row = [], []
    for label, value in pairs:
        row += [_p(label, SMALL), _p(value or "-", CELL)]
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row + [_p(""), _p("")])
    table = Table(rows, colWidths=[width * 0.16, width * 0.34, width * 0.16, width * 0.34])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, RULE),
        ("BACKGROUND", (0, 0), (0, -1), ZEBRA),
        ("BACKGROUND", (2, 0), (2, -1), ZEBRA),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _grid(header: list[str], rows: list[list[Any]], widths: list[float], right_cols: tuple[int, ...] = ()) -> Table:
    data = [[_p(text, ParagraphStyle("h", parent=CELL, textColor=colors.white, fontName="Helvetica-Bold")) for text in header]]
    for row in rows:
        data.append([_p(value, CELL_R if index in right_cols else CELL) for index, value in enumerate(row)])
    table = Table(data, colWidths=widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.3, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    style += [("BACKGROUND", (0, index), (-1, index), ZEBRA) for index in range(2, len(data), 2)]
    table.setStyle(TableStyle(style))
    return table


def _footer(canvas, document) -> None:
    canvas.saveState()
    canvas.setStrokeColor(RULE)
    canvas.line(document.leftMargin, 14 * mm, A4[0] - document.rightMargin, 14 * mm)
    canvas.setFont("Helvetica", 7.2)
    canvas.setFillColor(MUTED)
    canvas.drawString(document.leftMargin, 10 * mm, "NuuCare Clinical Decision Platform - demo data, not a payer-ready document")
    canvas.drawRightString(A4[0] - document.rightMargin, 10 * mm, f"Page {document.page}")
    canvas.restoreState()


def _render(title: str, story: list, subject: str) -> bytes:
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=15 * mm, bottomMargin=20 * mm,
        title=title, author="NuuCare", subject=subject, creator="NuuCare", invariant=1,
    )
    document.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Draft bill (Billing & RCM page)
# ---------------------------------------------------------------------------
def bill_pdf_bytes(export: dict[str, Any], record: dict[str, Any] | None = None) -> bytes:
    record = record or {}
    width = A4[0] - 32 * mm
    lines = export.get("lines") or []
    story: list = [
        Paragraph("Draft Hospital Bill", TITLE),
        Paragraph(escape(f"Encounter {export.get('encounter_id', '')} · generated {date.today():%d %b %Y}"), SUBTITLE),
        _key_value_table([
            ("Patient", export.get("patient")),
            ("Encounter ID", export.get("encounter_id")),
            ("Encounter type", export.get("encounter_type")),
            ("Consultation", record.get("id")),
            ("Doctor", record.get("doctor")),
            ("Visit date", " ".join(filter(None, [_t(record.get("date")), _t(record.get("time"))]))),
            ("Note status", record.get("status")),
            ("Released by", record.get("bill_released_by")),
        ], width),
        Paragraph("Billable services", H2),
    ]
    if lines:
        story.append(_grid(
            ["CPT/HCPCS", "Service", "Department", "Qty", "Unit charge", "Gross charge", "Pricing"],
            [[line.get("CPT/HCPCS Code"), line.get("Service Description"), line.get("Department"), line.get("Quantity"),
              line.get("Unit Charge"), line.get("Gross Charge"), line.get("Pricing Status")] for line in lines],
            [width * w for w in (0.11, 0.30, 0.15, 0.06, 0.12, 0.13, 0.13)],
            right_cols=(3, 4, 5),
        ))
    else:
        story.append(Paragraph("No billable services.", BODY))
    story += [Spacer(1, 5), Paragraph(f"<b>Gross total: {_money(export.get('gross_total'))}</b>", TOTAL)]
    held = export.get("held_lines") or []
    if held:
        story += [Paragraph("Held by the billing guardrail (not billed)", H2),
                  _grid(["CPT/HCPCS", "Reason"], [[row.get("code"), row.get("reason")] for row in held],
                        [width * 0.2, width * 0.8])]
    story += [Spacer(1, 10), KeepTogether([
        Paragraph("Notice", H2),
        Paragraph(escape(_t(export.get("notice"))), ParagraphStyle("warn", parent=BODY, textColor=WARN)),
    ])]
    return _render(f"Draft bill {export.get('encounter_id', '')}", story, "Draft hospital bill")


# ---------------------------------------------------------------------------
# Claim (Claim Generation page)
# ---------------------------------------------------------------------------
def claim_pdf_bytes(claim: dict[str, Any]) -> bytes:
    width = A4[0] - 32 * mm
    professional = claim.get("Claim Type") == "837P"
    patient = claim.get("Patient Information") if isinstance(claim.get("Patient Information"), dict) else {}
    story: list = [
        Paragraph(f"{escape(_t(claim.get('Claim Type')))} Claim Draft", TITLE),
        Paragraph(escape(f"Claim {claim.get('Claim ID', '')} · {claim.get('Status', '')} · "
                         f"{'Professional' if professional else 'Institutional / facility'}"), SUBTITLE),
        _key_value_table([
            ("Claim ID", claim.get("Claim ID")),
            ("Encounter ID", claim.get("Encounter ID")),
            ("Encounter type", claim.get("Encounter Type") or ("OPD" if professional else "IPD")),
            ("Status", claim.get("Status")),
            ("Billing entity", claim.get("Billing Entity")),
            ("Documents", (claim.get("Supporting Documents Validation") or {}).get("Overall Status")),
        ], width),
        Paragraph("Patient", H2),
        _key_value_table(list(patient.items()), width),
    ]
    if professional:
        story += [Paragraph("Rendering provider", H2), _key_value_table([
            *((claim.get("Rendering Provider") or {}).items()),
            ("Place of service", claim.get("Place of Service")),
        ], width)]
    else:
        story += [Paragraph("Facility", H2), _key_value_table([
            *((claim.get("Facility Information") or {}).items()),
            ("Admission", claim.get("Admission Date")), ("Discharge", claim.get("Discharge Date")),
            ("Bill type", claim.get("Bill Type")), ("Revenue codes", claim.get("Revenue Codes")),
        ], width)]
    story += [Paragraph("Diagnoses and procedures", H2), _key_value_table([
        ("ICD-10-CM", claim.get("ICD-10 Codes") or "Not recorded"),
        ("CPT/HCPCS", claim.get("CPT/HCPCS Codes") or "Not recorded"),
    ], width)]
    charges = claim.get("Charges") or []
    story.append(Paragraph("Professional charges" if professional else "Facility charges", H2))
    story.append(_grid(
        ["CPT/HCPCS", "Service", "Qty", "Amount", "Dx pointers" if professional else "Revenue code"],
        [[charge.get("CPT/HCPCS Codes"), charge.get("Service Description"), charge.get("Quantity"),
          _money(charge.get("Charge Amount ($)")),
          charge.get("Diagnosis Pointers") if professional else charge.get("Revenue Code")] for charge in charges]
        or [["-", "No charges", "", "", ""]],
        [width * w for w in (0.16, 0.42, 0.08, 0.14, 0.20)], right_cols=(2, 3),
    ))
    amount = claim.get("Claim Amount", claim.get("Total Facility Amount", claim.get("Total Claim Amount")))
    story += [Spacer(1, 5), Paragraph(f"<b>Claim amount: {_money(amount)}</b>", TOTAL)]
    results = (claim.get("Supporting Documents Validation") or {}).get("Results") or []
    if results:
        story += [Paragraph("Supporting clinical documents", H2), _grid(
            ["CPT", "Procedure", "Required document", "Status"],
            [[row.get("CPT Code"), row.get("Procedure"), row.get("Required Document"), row.get("Status")] for row in results],
            [width * w for w in (0.12, 0.38, 0.32, 0.18)],
        )]
    soap = _t((claim.get("Clinical Context") or {}).get("SOAP Note"))
    if soap:
        story += [Paragraph("Clinical documentation (SOAP)", H2), _p(soap[:4000], BODY)]
    story += [Spacer(1, 8), Paragraph("POC draft claim record; not a payer-ready X12 submission.", SMALL)]
    return _render(f"Claim {claim.get('Claim ID', '')}", story, f"{claim.get('Claim Type', '')} claim draft")


# ---------------------------------------------------------------------------
# Archiving (download_button on_click callback)
# ---------------------------------------------------------------------------


def archive_pdf(document_type: str, file_name: str, content: bytes, actor: str = "", **details: Any) -> dict[str, Any]:
    """Store a generated PDF in BILLING.BILL_DOCUMENTS. Returns {"archived": bool, "created": bool, "error": str}."""
    from app.db.settings import use_snowflake

    if not use_snowflake():
        return {"archived": False, "created": False, "error": "Database archive is only available on the Snowflake backend"}
    from app.db import billing as billing_repo
    from app.db.client import StorageError

    try:
        result = billing_repo.archive_document(document_type, file_name, content, actor=actor, **details)
        return {"archived": True, "created": bool(result.get("created")), "error": ""}
    except StorageError as error:
        return {"archived": False, "created": False, "error": str(error)}
