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

APP_VERSION = "0.1.0"
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
    sv_gross: float | None = Field(default=None, ge=0)
    stated_net: float = Field(ge=-1000000)
    wage_tax: float = Field(default=0, ge=0)
    solidarity_surcharge: float = Field(default=0, ge=0)
    church_tax: float = Field(default=0, ge=0)
    pension_employee: float = Field(default=0, ge=0)
    unemployment_employee: float = Field(default=0, ge=0)
    health_employee: float = Field(default=0, ge=0)
    care_employee: float = Field(default=0, ge=0)
    other_deductions: float = Field(default=0, ge=0)
    other_net_additions: float = Field(default=0, ge=0)
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

    # 2026 assessment ceilings used for this preview engine.
    rv_av_ceiling = 8450.00
    kv_pv_ceiling = 5812.50

    rv_basis = min(sv_gross, rv_av_ceiling)
    kv_basis = min(sv_gross, kv_pv_ceiling)

    expected_pension = rv_basis * 0.093
    expected_unemployment = rv_basis * 0.013
    expected_health = kv_basis * ((7.3 + data.health_additional_rate / 2) / 100)
    expected_care = kv_basis * (data.care_employee_rate / 100)

    calculated_net = (
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

    comparisons = [
        compare("Rentenversicherung Arbeitnehmer", data.pension_employee, expected_pension, data.tolerance),
        compare("Arbeitslosenversicherung Arbeitnehmer", data.unemployment_employee, expected_unemployment, data.tolerance),
        compare("Krankenversicherung Arbeitnehmer", data.health_employee, expected_health, data.tolerance),
        compare("Pflegeversicherung Arbeitnehmer", data.care_employee, expected_care, data.tolerance),
        compare("Netto-Rechenkette", data.stated_net, calculated_net, data.tolerance),
    ]

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
        "sv_gross": money(sv_gross),
        "stated_net": money(data.stated_net),
        "calculated_net": money(calculated_net),
        "net_difference": money(data.stated_net - calculated_net),
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
            "Preview-Prüfung: Die Netto-Rechenkette und ausgewählte Sozialversicherungsbeiträge werden geprüft. "
            "Lohnsteuer, Sonderfälle, Midijob, Mehrfachbeschäftigung, Sachsen-Regelung, Kinderabschläge in der "
            "Pflegeversicherung, Einmalzahlungen und tarifliche Zuschläge werden noch nicht vollständig automatisch berechnet."
        ),
    }

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
                money(calculated_net),
                money(data.stated_net - calculated_net),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        conn.commit()

    return result


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"version": APP_VERSION},
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "app": "LohnCheck",
        "version": APP_VERSION,
        "database": str(DB_PATH),
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    with db_connect() as conn:
        checks = conn.execute("SELECT COUNT(*) AS c FROM checks").fetchone()["c"]
        uploads = conn.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"]
    return {
        "version": APP_VERSION,
        "checks": checks,
        "uploads": uploads,
        "data_dir": str(APP_DATA_DIR),
    }


@app.post("/api/check")
def check_payroll(data: PayrollCheckInput) -> dict[str, Any]:
    return perform_check(data)


@app.get("/api/history")
def history(limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, payroll_month, gross, stated_net, calculated_net, difference
            FROM checks ORDER BY id DESC LIMIT ?
            """,
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
    except Exception as exc:  # keep upload usable even for malformed/scanned PDFs
        extraction_error = str(exc)

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO uploads(created_at, original_name, stored_name, extracted_chars)
            VALUES (?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                original_name,
                stored_name,
                len(extracted_text),
            ),
        )
        conn.commit()

    sample = extracted_text[:1800].strip()
    return {
        "ok": True,
        "original_name": original_name,
        "stored_name": stored_name,
        "stored_path": str(target),
        "extracted_chars": len(extracted_text),
        "text_sample": sample,
        "extraction_error": extraction_error,
        "message": (
            "PDF lokal gespeichert und Text erkannt. Die automatische Zuordnung von Lohnarten folgt in der nächsten Ausbaustufe."
            if extracted_text
            else "PDF lokal gespeichert. Kein Text-Layer erkannt; für Scan-PDFs wird später OCR ergänzt."
        ),
    }
