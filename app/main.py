from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pypdf import PdfReader

APP_VERSION = "0.2.0"
APP_DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/data"))
DOCUMENT_DIR = APP_DATA_DIR / "documents"
DB_PATH = APP_DATA_DIR / "payroll_checker.sqlite3"
TEMPLATES_DIR = Path(__file__).parent / "templates"

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
DOCUMENT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LohnCheck", version=APP_VERSION)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                payroll_month TEXT,
                gross REAL NOT NULL,
                stated_net REAL NOT NULL,
                calculated_net REAL NOT NULL,
                difference REAL NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                extracted_chars INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


init_db()


class PayrollCheckInput(BaseModel):
    payroll_month: str = ""
    gross: float = Field(ge=0)
    tax_gross: float | None = Field(default=None, ge=0)
    sv_gross: float | None = Field(default=None, ge=0)
    stated_net: float = Field(ge=-1000000)
    stated_payout: float | None = Field(default=None, ge=-1000000)
    wage_tax: float = Field(default=0, ge=0)
    solidarity_surcharge: float = Field(default=0, ge=0)
    church_tax: float = Field(default=0, ge=0)
    pension_employee: float = Field(default=0, ge=0)
    unemployment_employee: float = Field(default=0, ge=0)
    health_employee: float = Field(default=0, ge=0)
    care_employee: float = Field(default=0, ge=0)
    other_deductions: float = Field(default=0, ge=0)
    other_net_additions: float = Field(default=0, ge=0)
    payout_deductions: float = Field(default=0, ge=0)
    payout_additions: float = Field(default=0, ge=0)
    health_additional_rate: float = Field(default=2.9, ge=0, le=20)
    care_employee_rate: float = Field(default=1.8, ge=0, le=10)
    tolerance: float = Field(default=0.05, ge=0, le=10)


def money(value: float) -> float:
    return round(float(value) + 1e-10, 2)


def compare(name: str, stated: float, expected: float, tolerance: float) -> dict[str, Any]:
    stated = money(stated)
    expected = money(expected)
    diff = money(stated - expected)
    abs_diff = abs(diff)
    if abs_diff <= tolerance:
        level = "ok"
        message = "stimmt innerhalb der Rundungstoleranz"
    elif abs_diff <= 1.0:
        level = "warning"
        message = "kleine Abweichung – Rundung oder Abrechnungsbesonderheit möglich"
    else:
        level = "error"
        message = "auffällige Abweichung – bitte prüfen"
    return {
        "name": name,
        "stated": stated,
        "expected": expected,
        "difference": diff,
        "level": level,
        "message": message,
    }


def perform_check(data: PayrollCheckInput) -> dict[str, Any]:
    sv_gross = data.sv_gross if data.sv_gross is not None else data.gross

    rv_av_ceiling = 8450.00
    kv_pv_ceiling = 5812.50
    rv_basis = min(sv_gross, rv_av_ceiling)
    kv_basis = min(sv_gross, kv_pv_ceiling)

    expected_pension = rv_basis * 0.093
    expected_unemployment = rv_basis * 0.013
    expected_health = kv_basis * ((7.3 + data.health_additional_rate / 2) / 100)
    expected_care = kv_basis * (data.care_employee_rate / 100)

    comparisons = [
        compare("Rentenversicherung Arbeitnehmer", data.pension_employee, expected_pension, data.tolerance),
        compare("Arbeitslosenversicherung Arbeitnehmer", data.unemployment_employee, expected_unemployment, data.tolerance),
        compare("Krankenversicherung Arbeitnehmer", data.health_employee, expected_health, data.tolerance),
        compare("Pflegeversicherung Arbeitnehmer", data.care_employee, expected_care, data.tolerance),
    ]

    # The legal net cannot always be reconstructed from Gesamtbrutto alone. Payrolls can
    # contain taxable/SV additions, salary conversions and non-cash benefits. Therefore
    # v0.2 validates the payout chain separately and marks the simple net chain as a preview.
    simple_net = (
        data.gross
        - data.wage_tax
        - data.solidarity_surcharge
        - data.church_tax
        - data.pension_employee
        - data.unemployment_employee
        - data.health_employee
        - data.care_employee
        - data.other_deductions
        + data.other_net_additions
    )

    if data.stated_payout is not None:
        expected_payout = data.stated_net - data.payout_deductions + data.payout_additions
        comparisons.append(compare("Auszahlungskette", data.stated_payout, expected_payout, data.tolerance))
    else:
        expected_payout = None

    errors = sum(1 for item in comparisons if item["level"] == "error")
    warnings = sum(1 for item in comparisons if item["level"] == "warning")
    if errors:
        overall = "error"
        overall_text = "Abweichungen gefunden"
    elif warnings:
        overall = "warning"
        overall_text = "weitgehend plausibel, einzelne Punkte prüfen"
    else:
        overall = "ok"
        overall_text = "innerhalb der aktuellen Prüflogik plausibel"

    result = {
        "version": APP_VERSION,
        "overall": overall,
        "overall_text": overall_text,
        "payroll_month": data.payroll_month,
        "gross": money(data.gross),
        "tax_gross": money(data.tax_gross) if data.tax_gross is not None else None,
        "sv_gross": money(sv_gross),
        "stated_net": money(data.stated_net),
        "simple_net_preview": money(simple_net),
        "stated_payout": money(data.stated_payout) if data.stated_payout is not None else None,
        "calculated_payout": money(expected_payout) if expected_payout is not None else None,
        "comparisons": comparisons,
        "parameters": {
            "year": 2026,
            "rv_av_ceiling_monthly": rv_av_ceiling,
            "kv_pv_ceiling_monthly": kv_pv_ceiling,
            "pension_employee_rate": 9.3,
            "unemployment_employee_rate": 1.3,
            "health_base_employee_rate": 7.3,
            "health_additional_rate": data.health_additional_rate,
            "care_employee_rate": data.care_employee_rate,
            "tolerance": data.tolerance,
        },
        "notice": (
            "v0.2 trennt gesetzliches Netto und tatsächliche Auszahlung. Die Sozialversicherungsbeiträge und die "
            "Auszahlungskette werden geprüft. Das gesetzliche Netto wird bei Entgeltumwandlung, geldwerten Vorteilen, "
            "Zusatzversorgung und weiteren Sonderfällen noch nicht vollständig aus den Bruttowerten neu berechnet."
        ),
    }

    stored_difference = 0.0
    stored_calculated = data.stated_net
    if expected_payout is not None and data.stated_payout is not None:
        stored_difference = money(data.stated_payout - expected_payout)
        stored_calculated = money(expected_payout)

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO checks(created_at, payroll_month, gross, stated_net, calculated_net, difference, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                data.payroll_month,
                money(data.gross),
                money(data.stated_net),
                stored_calculated,
                stored_difference,
                json.dumps(result, ensure_ascii=False),
            ),
        )
        conn.commit()

    return result


def parse_de_number(value: str | None) -> float | None:
    if not value:
        return None
    cleaned = value.strip().replace("€", "").replace(" ", "")
    sign = -1 if cleaned.startswith("-") else 1
    cleaned = cleaned.lstrip("+-")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return money(sign * float(cleaned))
    except ValueError:
        return None


def first_amount(text: str, patterns: list[str]) -> float | None:
    amount = r"(-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+,\d{2})"
    for pattern in patterns:
        match = re.search(pattern + r"[^\n\r]*?" + amount, text, flags=re.IGNORECASE)
        if match:
            return parse_de_number(match.group(1))
    return None


def normalize_month(text: str) -> str:
    match = re.search(r"Abrechnungsmonat\s+([A-Za-zÄÖÜäöüß]+)\s+(20\d{2})", text, re.IGNORECASE)
    if not match:
        return ""
    months = {
        "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
        "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
        "oktober": 10, "november": 11, "dezember": 12,
    }
    month = months.get(match.group(1).lower())
    return f"{month:02d}/{match.group(2)}" if month else f"{match.group(1)} {match.group(2)}"


def redact_sensitive_text(text: str) -> str:
    redacted = text
    substitutions = [
        (r"(Steuer-Identifikationsnr\.?\s*:\s*)\S+", r"\1[GESCHÜTZT]"),
        (r"(SV-Nr\.?\s*:\s*)\S+", r"\1[GESCHÜTZT]"),
        (r"(ZVK-Nummer\s*)\S+", r"\1[GESCHÜTZT]"),
        (r"(Geburtsdatum\s*:\s*)\S+", r"\1[GESCHÜTZT]"),
        (r"\bDE\d{20}\b", "[IBAN GESCHÜTZT]"),
        (r"\b[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b", "[BIC GESCHÜTZT]"),
    ]
    for pattern, replacement in substitutions:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    return redacted


def parse_payroll_text(text: str) -> dict[str, Any]:
    gross = first_amount(text, [r"\bBRG\s+Gesamtbrutto\b", r"\bGesamtbrutto\b"])
    tax_gross = first_amount(text, [r"\bBSL\s+Steuerbrutto[^\n]*", r"\bSteuerbrutto[^\n]*"])
    kv_gross = first_amount(text, [r"\bBRK\s+Krankenversicherungsbrutto\b", r"\bKrankenversicherungsbrutto\b"])
    rv_gross = first_amount(text, [r"\bBRR\s+Rentenversicherungsbrutto\b", r"\bRentenversicherungsbrutto\b"])
    sv_gross = rv_gross if rv_gross is not None else kv_gross

    wage_tax = abs(first_amount(text, [r"\bLST\s+Lohnsteuer[^\n]*", r"\bLohnsteuer[^\n]*"]) or 0)
    soli = abs(first_amount(text, [r"Solidaritätszuschlag", r"Solidaritaetszuschlag"]) or 0)
    church = abs(first_amount(text, [r"Kirchensteuer"]) or 0)
    pension = abs(first_amount(text, [r"\bRAN\s+Rentenversicherung[^\n]*"]) or 0)
    unemployment = abs(first_amount(text, [r"\bAAN\s+Arbeitslosenversicherung[^\n]*"]) or 0)
    health_base = abs(first_amount(text, [r"\bKAN\s+Krankenversicherung[^\n]*"]) or 0)
    health_additional = abs(first_amount(text, [r"\bKZA\s+Krankenversicherung Zusatzbeitrag[^\n]*"]) or 0)
    health = money(health_base + health_additional)
    care_base = abs(first_amount(text, [r"\bPAN\s+Pflegeversicherung[^\n]*"]) or 0)
    care_childless = abs(first_amount(text, [r"\bPA9\s+PV-Kinderlosenzuschlag[^\n]*"]) or 0)
    care = money(care_base + care_childless)

    legal_net = first_amount(text, [r"\bGSN\s+Gesetzliches Netto\b", r"\bGesetzliches Netto\b"])
    payout = first_amount(text, [r"\bAZB\s+Auszahlungsbetrag\b", r"\bAuszahlungsbetrag\b"])
    supplementary = first_amount(text, [r"\bZVA\s+Zusatzversorgung\b"])
    gwv = first_amount(text, [r"\bGWS\s+Summe Abzug GWV\b"])
    payout_correction = first_amount(text, [r"\bAZR\s+Auszahlungskorrektur[^\n]*"])

    payout_deductions = money(abs(min(supplementary or 0, 0)) + abs(min(gwv or 0, 0)))
    payout_additions = money(max(payout_correction or 0, 0))

    inferred_health_additional_rate = None
    if kv_gross and health_additional:
        inferred_health_additional_rate = round((health_additional / kv_gross) * 200, 4)

    inferred_care_rate = None
    if kv_gross and care:
        inferred_care_rate = round((care / kv_gross) * 100, 4)

    salary_items = []
    item_pattern = re.compile(
        r"(?m)^\s*(\d{3})\s+([^\n]+?)\s+(-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+,\d{2})(?:\s+(-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+,\d{2}))?\s*$"
    )
    for match in item_pattern.finditer(text):
        code, label, amount1, amount2 = match.groups()
        current = parse_de_number(amount2 or amount1)
        if current is not None:
            salary_items.append({"code": code, "label": label.strip(), "amount": current})

    fields = {
        "payroll_month": normalize_month(text),
        "gross": gross,
        "tax_gross": tax_gross,
        "sv_gross": sv_gross,
        "stated_net": legal_net,
        "stated_payout": payout,
        "wage_tax": wage_tax,
        "solidarity_surcharge": soli,
        "church_tax": church,
        "pension_employee": pension,
        "unemployment_employee": unemployment,
        "health_employee": health,
        "care_employee": care,
        "other_deductions": 0.0,
        "other_net_additions": 0.0,
        "payout_deductions": payout_deductions,
        "payout_additions": payout_additions,
    }
    if inferred_health_additional_rate is not None:
        fields["health_additional_rate"] = inferred_health_additional_rate
    if inferred_care_rate is not None:
        fields["care_employee_rate"] = inferred_care_rate

    detected = sum(1 for value in fields.values() if value not in (None, "", 0.0))
    return {
        "fields": fields,
        "salary_items": salary_items[:50],
        "details": {
            "kv_gross": kv_gross,
            "rv_gross": rv_gross,
            "health_base": health_base,
            "health_additional": health_additional,
            "care_base": care_base,
            "care_childless": care_childless,
            "supplementary_pension": supplementary,
            "gwv_deduction": gwv,
            "payout_correction": payout_correction,
        },
        "detected_fields": detected,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"version": APP_VERSION})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "app": "LohnCheck", "version": APP_VERSION, "database": str(DB_PATH)}


@app.get("/api/status")
def status() -> dict[str, Any]:
    with db_connect() as conn:
        checks = conn.execute("SELECT COUNT(*) AS c FROM checks").fetchone()["c"]
        uploads = conn.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"]
    return {"version": APP_VERSION, "checks": checks, "uploads": uploads, "data_dir": str(APP_DATA_DIR)}


@app.post("/api/check")
def check_payroll(data: PayrollCheckInput) -> dict[str, Any]:
    return perform_check(data)


@app.get("/api/history")
def history(limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, payroll_month, gross, stated_net, calculated_net, difference FROM checks ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def safe_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return base[:120] or "payroll.pdf"


@app.post("/api/upload")
def upload_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
    original_name = file.filename or "payroll.pdf"
    if not original_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Nur PDF-Dateien werden unterstützt.")

    stored_name = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}-{safe_filename(original_name)}"
    target = DOCUMENT_DIR / stored_name
    with target.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    extracted_text = ""
    extraction_error = None
    try:
        reader = PdfReader(str(target))
        extracted_text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        extraction_error = str(exc)

    parsed = parse_payroll_text(extracted_text) if extracted_text else {"fields": {}, "salary_items": [], "details": {}, "detected_fields": 0}

    with db_connect() as conn:
        conn.execute(
            "INSERT INTO uploads(created_at, original_name, stored_name, extracted_chars) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), original_name, stored_name, len(extracted_text)),
        )
        conn.commit()

    safe_sample = redact_sensitive_text(extracted_text[:3000]).strip()
    return {
        "ok": True,
        "original_name": original_name,
        "stored_name": stored_name,
        "stored_path": str(target),
        "extracted_chars": len(extracted_text),
        "text_sample": safe_sample,
        "extraction_error": extraction_error,
        "parsed": parsed,
        "message": (
            f"PDF lokal gespeichert. {parsed['detected_fields']} Abrechnungsfelder wurden automatisch erkannt."
            if extracted_text
            else "PDF lokal gespeichert. Kein Text-Layer erkannt; für Scan-PDFs wird später OCR ergänzt."
        ),
    }
