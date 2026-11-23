"""Official code-set sources.

ICD-10-CM  CDC / NCHS publishes the official release files used by CMS for HIPAA claims:
           https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/
             <YYYY>/          fiscal-year release, effective October 1 of YYYY-1
             <YYYY>-update/   mid-year update, effective April 1 of YYYY
           Each contains icd10cm-code-descriptions-<YYYY>.zip with the fixed-width "order" file
           (code, billable flag, short and long description).

HCPCS L-II CMS HCPCS file, served by the U.S. National Library of Medicine Clinical Tables API
           https://clinicaltables.nlm.nih.gov/api/hcpcs/v3/search  (includes add / termination dates)

CPT        Owned by the AMA and licence-restricted: no source is configured.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterator

import requests

CDC_ICD10CM_ROOT = "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/"
NLM_HCPCS_URL = "https://clinicaltables.nlm.nih.gov/api/hcpcs/v3/search"
USER_AGENT = "NuuCare-CodeSetSync/1.0"
TIMEOUT = (15, 180)


@dataclass(frozen=True)
class CodeRow:
    code: str            # display form
    code_raw: str        # no punctuation
    short_description: str
    long_description: str
    is_billable: bool
    source_valid_from: date | None = None
    source_valid_to: date | None = None

    def row_hash(self) -> str:
        parts = (self.code_raw, self.short_description, self.long_description, str(self.is_billable),
                 str(self.source_valid_to or ""))
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@dataclass
class Release:
    code_system: str
    release_id: str
    effective_from: date
    source: str
    source_url: str
    rows: list[CodeRow] = field(default_factory=list)

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256()
        for row in sorted(self.rows, key=lambda item: item.code_raw):
            digest.update(row.row_hash().encode("ascii"))
        return digest.hexdigest()


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


# ---------------------------------------------------------------------------
# ICD-10-CM (CDC / NCHS)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class IcdReleaseRef:
    release_id: str      # FY2027 | 2026-UPDATE
    folder: str          # 2027 | 2026-update
    year: int
    effective_from: date


def format_icd10cm(code_raw: str) -> str:
    code_raw = code_raw.strip().upper()
    return f"{code_raw[:3]}.{code_raw[3:]}" if len(code_raw) > 3 else code_raw


def discover_icd10cm_releases() -> list[IcdReleaseRef]:
    """All ICD-10-CM releases published by CDC, oldest first."""
    html = _session().get(CDC_ICD10CM_ROOT, timeout=TIMEOUT).text
    refs = {}
    for folder in re.findall(r'/ICD10CM/(\d{4}(?:-[Uu]pdate)?)/"', html):
        year = int(folder[:4])
        if "-" in folder:
            refs[folder] = IcdReleaseRef(f"{year}-UPDATE", folder, year, date(year, 4, 1))
        else:
            refs[folder] = IcdReleaseRef(f"FY{year}", folder, year, date(year - 1, 10, 1))
    return sorted(refs.values(), key=lambda ref: ref.effective_from)


def _find_code_descriptions_zip(folder: str) -> str:
    url = f"{CDC_ICD10CM_ROOT}{folder}/"
    html = _session().get(url, timeout=TIMEOUT).text
    names = re.findall(r'HREF="([^"]+\.zip)"', html, flags=re.IGNORECASE)
    candidates = [name for name in names if re.search(r"code[-_ ]?descriptions", name, re.IGNORECASE)]
    if not candidates:
        candidates = [name for name in names if re.search(r"order|codes?[-_]file|description", name, re.IGNORECASE)]
    if not candidates:
        raise LookupError(f"No ICD-10-CM code description archive found in {url}")
    name = candidates[0]
    return name if name.startswith("http") else "https://ftp.cdc.gov" + name if name.startswith("/") else url + name


def parse_icd10cm_order_file(text: str) -> list[CodeRow]:
    """CDC order file: cols 1-5 order, 7-13 code, 15 billable flag, 17-76 short, 78+ long."""
    rows = []
    for line in text.splitlines():
        if len(line) < 16 or not line[:5].strip().isdigit():
            continue
        code_raw = line[6:13].strip().upper()
        if not code_raw:
            continue
        short = line[16:76].strip()
        long_description = line[77:].strip() or short
        rows.append(CodeRow(format_icd10cm(code_raw), code_raw, short, long_description, line[14] == "1"))
    return rows


def fetch_icd10cm_release(ref: IcdReleaseRef) -> Release:
    url = _find_code_descriptions_zip(ref.folder)
    response = _session().get(url, timeout=TIMEOUT)
    response.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        order_files = [
            name for name in archive.namelist()
            if re.search(r"order[-_]?\d{4}\.txt$", name, re.IGNORECASE) and "addenda" not in name.lower()
        ]
        if not order_files:
            raise LookupError(f"No order file inside {url}")
        text = archive.read(order_files[0]).decode("latin-1")
    rows = parse_icd10cm_order_file(text)
    if len(rows) < 50000:  # sanity check: a real release has ~98k rows (~75k billable)
        raise ValueError(f"ICD-10-CM {ref.release_id} looks incomplete ({len(rows)} rows)")
    return Release("ICD10CM", ref.release_id, ref.effective_from, "CDC/NCHS ICD-10-CM", url, rows)


# ---------------------------------------------------------------------------
# HCPCS Level II (CMS via NLM Clinical Tables)
# ---------------------------------------------------------------------------
HCPCS_PREFIXES = "ABCDEGHJKLMPQRSTUV"
_HCPCS_FIELDS = "code,short_desc,long_desc,obsolete,add_dt,term_dt"
_HCPCS_CODE = re.compile(r"^[A-V]\d{4}$")


def _nlm_date(value: str) -> date | None:
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%Y%m%d").date() if value else None
    except ValueError:
        return None


def _hcpcs_pages(session: requests.Session, prefix: str) -> Iterator[list]:
    offset, page = 0, 500
    while True:
        response = session.get(NLM_HCPCS_URL, params={
            "terms": prefix, "sf": "code", "df": _HCPCS_FIELDS, "count": page, "offset": offset,
        }, timeout=TIMEOUT)
        if response.status_code == 400 and offset:
            return  # past the end
        response.raise_for_status()
        total, _codes, _extra, rows = response.json()
        yield rows
        offset += page
        if offset >= total or not rows:
            return
        time.sleep(0.1)  # be polite to a free public service


def fetch_hcpcs_release(today: date | None = None) -> Release:
    today = today or date.today()
    session = _session()
    rows: dict[str, CodeRow] = {}
    for prefix in HCPCS_PREFIXES:
        for page in _hcpcs_pages(session, prefix):
            for code, short, long_description, _obsolete, add_dt, term_dt in page:
                code = str(code).strip().upper()
                if not _HCPCS_CODE.match(code):
                    continue
                rows[code] = CodeRow(
                    code, code, str(short or "").strip(), str(long_description or short or "").strip(), True,
                    _nlm_date(add_dt), _nlm_date(term_dt),
                )
    if len(rows) < 3000:
        raise ValueError(f"HCPCS download looks incomplete ({len(rows)} codes)")
    return Release("HCPCS", f"NLM-{today:%Y-%m-%d}", today, "CMS HCPCS via NLM Clinical Tables",
                   NLM_HCPCS_URL, sorted(rows.values(), key=lambda row: row.code_raw))


# ---------------------------------------------------------------------------
# Live verification (used before claims go out)
# ---------------------------------------------------------------------------
NLM_ICD10CM_URL = "https://clinicaltables.nlm.nih.gov/api/icd10cm/v3/search"


def live_lookup(code_system: str, code: str) -> bool | None:
    """True/False if NLM confirms/denies the code exists today; None if NLM is unreachable."""
    url = NLM_ICD10CM_URL if code_system == "ICD10CM" else NLM_HCPCS_URL
    try:
        response = _session().get(url, params={"terms": code, "sf": "code", "count": 50}, timeout=(5, 10))
        response.raise_for_status()
        _total, codes, *_ = response.json()
    except (requests.RequestException, ValueError):
        return None
    wanted = code.replace(".", "").upper()
    return any(str(item).replace(".", "").upper() == wanted for item in codes)
