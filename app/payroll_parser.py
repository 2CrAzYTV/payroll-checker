from __future__ import annotations

import re
from typing import Any

LAST_TAX_CONTEXT: dict[str, Any] = {
    "tax_class": 1,
    "child_allowance": 0.0,
    "childless_care_surcharge": False,
    "church_tax_rate": 0.0,
    "saxony": False,
}


def money(value: float) -> float:
    return round(float(value) + 1e-10, 2)


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


AMOUNT_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+,\d{2}")


def code_line(text: str, code: str) -> str | None:
    match = re.search(rf"(?mi)^\s*{re.escape(code)}\s+([^\r\n]+)", text)
    return match.group(0).strip() if match else None


def amount_for_code(text: str, code: str, index: int = 0) -> float | None:
    line = code_line(text, code)
    if not line:
        return None
    tail = re.sub(rf"^\s*{re.escape(code)}\s+", "", line, flags=re.IGNORECASE)
    amounts = AMOUNT_RE.findall(tail)
    if len(amounts) <= index:
        return None
    return parse_de_number(amounts[index])


def normalize_month(text: str) -> str:
    match = re.search(r"Abrechnungsmonat\s+([A-Za-zÄÖÜäöüß]+)\s+(20\d{2})", text, re.IGNORECASE)
    if not match:
        return ""
    months = {"januar":1,"februar":2,"märz":3,"maerz":3,"april":4,"mai":5,"juni":6,"juli":7,"august":8,"september":9,"oktober":10,"november":11,"dezember":12}
    month = months.get(match.group(1).lower())
    return f"{month:02d}/{match.group(2)}" if month else f"{match.group(1)} {match.group(2)}"


def tax_context(text: str) -> dict[str, Any]:
    stkl_match = re.search(r"Steuerklasse\s*(\d)", text, re.IGNORECASE)
    tax_class = int(stkl_match.group(1)) if stkl_match else 1

    kfb_match = re.search(r"(?:kein\s+Kinderfreibetrag|Kinderfreibetrag\s*[:]?\s*([0-9]+(?:[,\.]\d+)?))", text, re.IGNORECASE)
    child_allowance = 0.0
    if kfb_match and kfb_match.group(1):
        child_allowance = float(kfb_match.group(1).replace(",", "."))

    childless = bool(re.search(r"PV-Kinderlosenzuschlag\s*:\s*Ja", text, re.IGNORECASE))
    not_church = bool(re.search(r"nicht\s+kirchensteuerpflichtig", text, re.IGNORECASE))
    # The parser currently defaults to Niedersachsen/non-Saxony when no explicit state is present.
    # Church tax is 0 for ELStAM 'nicht kirchensteuerpflichtig'.
    context = {
        "tax_class": tax_class,
        "child_allowance": child_allowance,
        "childless_care_surcharge": childless,
        "church_tax_rate": 0.0 if not_church else 9.0,
        "saxony": False,
    }
    return context


def parse_salary_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        match = re.match(r"^\s*(\d{3})\s+(.+)$", raw_line)
        if not match:
            continue
        code, tail = match.groups()
        amounts = AMOUNT_RE.findall(tail)
        if not amounts:
            continue
        last_amount = amounts[-1]
        pos = tail.rfind(last_amount)
        label = tail[pos + len(last_amount):].strip() if pos >= 0 else ""
        if not label:
            first_pos = tail.find(amounts[0])
            label = tail[:first_pos].strip() if first_pos >= 0 else ""
        current = parse_de_number(amounts[0])
        if current is not None:
            items.append({"code": code, "label": label, "amount": current})
    return items[:50]


def parse_payroll_text(text: str) -> dict[str, Any]:
    global LAST_TAX_CONTEXT
    LAST_TAX_CONTEXT = tax_context(text)

    gross = amount_for_code(text, "BRG")
    tax_gross = amount_for_code(text, "BSL")
    kv_gross = amount_for_code(text, "BRK")
    rv_gross = amount_for_code(text, "BRR")
    sv_gross = rv_gross if rv_gross is not None else kv_gross

    wage_tax = abs(amount_for_code(text, "LST") or 0)
    pension = abs(amount_for_code(text, "RAN") or 0)
    unemployment = abs(amount_for_code(text, "AAN") or 0)
    health_base = abs(amount_for_code(text, "KAN") or 0)
    health_additional = abs(amount_for_code(text, "KZA") or 0)
    health = money(health_base + health_additional)
    care_base = abs(amount_for_code(text, "PAN") or 0)
    care_childless = abs(amount_for_code(text, "PA9") or 0)
    care = money(care_base + care_childless)

    legal_net = amount_for_code(text, "GSN")
    payout = amount_for_code(text, "AZB")
    supplementary = amount_for_code(text, "ZVA")
    gwv = amount_for_code(text, "GWS")
    payout_correction = amount_for_code(text, "AZR")

    payout_deductions = money(abs(min(supplementary or 0, 0)) + abs(min(gwv or 0, 0)))
    payout_additions = money(max(payout_correction or 0, 0))

    inferred_health_additional_rate = round((health_additional / kv_gross) * 200, 4) if kv_gross and health_additional else None
    inferred_care_rate = round((care / kv_gross) * 100, 4) if kv_gross and care else None

    fields: dict[str, Any] = {
        "payroll_month": normalize_month(text), "gross": gross, "tax_gross": tax_gross, "sv_gross": sv_gross,
        "stated_net": legal_net, "stated_payout": payout, "wage_tax": wage_tax,
        "solidarity_surcharge": 0.0, "church_tax": 0.0, "pension_employee": pension,
        "unemployment_employee": unemployment, "health_employee": health, "care_employee": care,
        "other_deductions": 0.0, "other_net_additions": 0.0,
        "payout_deductions": payout_deductions, "payout_additions": payout_additions,
    }
    if inferred_health_additional_rate is not None:
        fields["health_additional_rate"] = inferred_health_additional_rate
    if inferred_care_rate is not None:
        fields["care_employee_rate"] = inferred_care_rate

    detected = sum(1 for value in fields.values() if value not in (None, "", 0.0))
    return {
        "fields": fields,
        "salary_items": parse_salary_items(text),
        "tax_context": LAST_TAX_CONTEXT.copy(),
        "details": {
            "kv_gross": kv_gross, "rv_gross": rv_gross, "health_base": health_base,
            "health_additional": health_additional, "care_base": care_base,
            "care_childless": care_childless, "supplementary_pension": supplementary,
            "gwv_deduction": gwv, "payout_correction": payout_correction,
        },
        "detected_fields": detected,
    }
