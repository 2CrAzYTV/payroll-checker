from __future__ import annotations

import itertools
import re
from typing import Any

LAST_TAX_CONTEXT: dict[str, Any] = {
    "tax_class": 1,
    "child_allowance": 0.0,
    "childless_care_surcharge": False,
    "church_tax_rate": 0.0,
    "saxony": False,
}
LAST_PAYROLL_DETAILS: dict[str, Any] = {}
LAST_SURCHARGE_ITEMS: list[dict[str, Any]] = []
LAST_WAGE_ITEMS: list[dict[str, Any]] = []
LAST_GROSS_COMPOSITION: dict[str, Any] = {}


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
    saxony = bool(re.search(r"\bSachsen\b", text, re.IGNORECASE))
    return {
        "tax_class": tax_class,
        "child_allowance": child_allowance,
        "childless_care_surcharge": childless,
        "church_tax_rate": 0.0 if not_church else 9.0,
        "saxony": saxony,
    }


def percent_from_label(label: str) -> float | None:
    match = re.search(r"(\d+(?:[,.]\d+)?)\s*%", label)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def classify_surcharge(label: str) -> tuple[str | None, float | None]:
    value = label.lower()
    stated_percent = percent_from_label(label)
    if any(x in value for x in ("nacht", "night")):
        return "Nachtarbeit", stated_percent if stated_percent is not None else 25.0
    if any(x in value for x in ("sonntag", "sonntags", "sonntagsarbeit")):
        return "Sonntagsarbeit", stated_percent if stated_percent is not None else 50.0
    if any(x in value for x in ("feiertag", "feiertags")):
        return "Feiertagsarbeit", stated_percent if stated_percent is not None else 125.0
    if any(x in value for x in ("samstag", "samstags")):
        return "Samstagsarbeit", stated_percent
    if any(x in value for x in ("überstund", "ueberstund", "mehrarbeit")):
        return "Überstunden/Mehrarbeit", stated_percent
    return None, stated_percent


def classify_quantity_wage_item(code: str, label: str) -> str | None:
    value = label.lower()
    known_codes = {"331", "332", "334"}
    if code in known_codes or any(x in value for x in (
        "geteilter dienst", "mankogeld", "stück", "stueck", "stk", "pauschale pro",
    )):
        return "Mengenlohnart"
    return None


def classify_gross_component(code: str, label: str) -> str | None:
    """Classify known payroll rows that contribute to the displayed gross reconciliation.

    This is intentionally conservative: only rows with semantics known from the source
    payroll are included. Informational rows such as insolvency insurance are excluded.
    """
    value = label.lower()
    if "info" in value:
        return None
    known = {
        "050": "Monatsentgelt",
        "064": "Nachtzuschlag",
        "067": "Samstagszuschlag",
        "331": "Geteilter Dienst BV",
        "332": "Mankogeld",
        "334": "Geteilter Dienst",
        "570": "Entgeltumwandlung PensFlex",
        "635": "Gehaltsumwandlung Dienstrad",
        "636": "Geldwerter Vorteil Dienstrad",
    }
    if code in known:
        return known[code]
    return None


def _extract_label(tail: str, amounts_raw: list[str]) -> str:
    first_pos = tail.find(amounts_raw[0])
    last_raw = amounts_raw[-1]
    last_pos = tail.rfind(last_raw)
    before = tail[:first_pos].strip() if first_pos >= 0 else ""
    after = tail[last_pos + len(last_raw):].strip() if last_pos >= 0 else ""

    before_cat, _ = classify_surcharge(before)
    after_cat, _ = classify_surcharge(after)
    if before_cat:
        return before
    if after_cat:
        return after

    def useful(value: str) -> bool:
        cleaned = re.sub(r"\b(?:Std\.?|Tage?)\b", "", value, flags=re.IGNORECASE).strip(" ()")
        return bool(re.search(r"[A-Za-zÄÖÜäöüß]{3,}", cleaned))

    if useful(before):
        return before
    if useful(after):
        return after
    return before or after


def parse_salary_items(text: str) -> list[dict[str, Any]]:
    """Parse LA rows preserving quantity, unit value, month amount and annual value."""
    items: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        match = re.match(r"^\s*(\d{3})\s+(.+)$", raw_line)
        if not match:
            continue
        code, tail = match.groups()
        amounts_raw = AMOUNT_RE.findall(tail)
        if not amounts_raw:
            continue
        amounts = [x for x in (parse_de_number(v) for v in amounts_raw) if x is not None]
        if not amounts:
            continue

        label = _extract_label(tail, amounts_raw)
        category, surcharge_percent = classify_surcharge(label)
        wage_item_type = classify_quantity_wage_item(code, label)
        gross_component_type = classify_gross_component(code, label)

        quantity = None
        unit_or_basis = None
        current_amount = None
        annual_value = None

        if len(amounts) >= 4:
            quantity = amounts[0]
            unit_or_basis = amounts[1]
            current_amount = amounts[-2]
            annual_value = amounts[-1]
        elif len(amounts) == 3:
            if abs(money(amounts[0] * amounts[1]) - money(amounts[2])) <= 0.02:
                quantity, unit_or_basis, current_amount = amounts
            else:
                current_amount = amounts[-2]
                annual_value = amounts[-1]
        elif len(amounts) == 2:
            current_amount, annual_value = amounts
        else:
            current_amount = amounts[0]

        calculated_amount = None
        calculation_kind = None
        if quantity is not None and unit_or_basis is not None:
            if category and surcharge_percent is not None:
                calculated_amount = money(quantity * unit_or_basis * surcharge_percent / 100.0)
                calculation_kind = "percentage"
            elif wage_item_type:
                calculated_amount = money(quantity * unit_or_basis)
                calculation_kind = "quantity_x_unit"

        item = {
            "code": code,
            "label": label,
            "amount": current_amount,
            "current_amount": current_amount,
            "annual_value": annual_value,
            "quantity": quantity,
            "unit_or_basis": unit_or_basis,
            "rate_or_basis": unit_or_basis,
            "calculated_amount": calculated_amount,
            "calculation_kind": calculation_kind,
            "values": amounts,
            "category": category,
            "wage_item_type": wage_item_type,
            "gross_component_type": gross_component_type,
            "surcharge_percent": surcharge_percent,
            "tax_free_percent": surcharge_percent,
        }
        items.append(item)
    return items[:80]


def _find_amount_subset(items: list[dict[str, Any]], target: float, tolerance: float = 0.02) -> list[int]:
    """Find the smallest subset whose current amounts reconcile a target difference."""
    candidates = [
        (idx, float(item.get("current_amount") or 0))
        for idx, item in enumerate(items)
        if 0 < float(item.get("current_amount") or 0) <= 500
    ]
    for size in range(1, min(6, len(candidates)) + 1):
        for combo in itertools.combinations(candidates, size):
            if abs(money(sum(value for _, value in combo)) - money(target)) <= tolerance:
                return [idx for idx, _ in combo]
    return []


def build_gross_composition(
    salary_items: list[dict[str, Any]],
    gross: float | None,
    tax_gross: float | None,
    sv_gross: float | None,
    zvs_sv_addition: float | None,
) -> dict[str, Any]:
    components = [
        dict(item)
        for item in salary_items
        if item.get("gross_component_type") and item.get("current_amount") is not None
    ]
    reconstructed_gross = money(sum(float(item.get("current_amount") or 0) for item in components))
    gross_difference = money((gross or 0) - reconstructed_gross) if gross is not None else None

    tax_delta = money((gross or 0) - (tax_gross or 0)) if gross is not None and tax_gross is not None else None
    inferred_indexes: list[int] = []
    if tax_delta is not None and tax_delta > 0:
        inferred_indexes = _find_amount_subset(components, tax_delta)

    inferred_tax_excluded = []
    for idx, item in enumerate(components):
        excluded = idx in inferred_indexes
        item["tax_effect"] = "aus Steuerbrutto herausgerechnet (hergeleitet)" if excluded else "im Steuerbrutto enthalten (hergeleitet)"
        item["tax_excluded_inferred"] = excluded
        if excluded:
            inferred_tax_excluded.append({
                "code": item.get("code"),
                "label": item.get("label"),
                "amount": item.get("current_amount"),
            })

    reconstructed_tax_gross = None
    tax_difference = None
    if gross is not None and tax_delta is not None and inferred_indexes:
        reconstructed_tax_gross = money(gross - sum(float(components[i].get("current_amount") or 0) for i in inferred_indexes))
        tax_difference = money((tax_gross or 0) - reconstructed_tax_gross) if tax_gross is not None else None

    reconstructed_sv_gross = None
    sv_difference = None
    if tax_gross is not None and zvs_sv_addition is not None:
        reconstructed_sv_gross = money(tax_gross + zvs_sv_addition)
        sv_difference = money((sv_gross or 0) - reconstructed_sv_gross) if sv_gross is not None else None

    return {
        "components": components,
        "reconstructed_gross": reconstructed_gross,
        "gross_difference": gross_difference,
        "tax_delta": tax_delta,
        "inferred_tax_excluded": inferred_tax_excluded,
        "reconstructed_tax_gross": reconstructed_tax_gross,
        "tax_difference": tax_difference,
        "zvs_sv_addition": zvs_sv_addition,
        "reconstructed_sv_gross": reconstructed_sv_gross,
        "sv_difference": sv_difference,
        "inference_note": (
            "Die Zuordnung von Einzelpositionen zur Differenz zwischen Gesamt- und Steuerbrutto wird aus den Beträgen der konkreten Abrechnung hergeleitet. "
            "Sie ist keine allgemeine steuerrechtliche Klassifizierung dieser Lohnart."
        ),
    }


def parse_payroll_text(text: str) -> dict[str, Any]:
    global LAST_TAX_CONTEXT, LAST_PAYROLL_DETAILS, LAST_SURCHARGE_ITEMS, LAST_WAGE_ITEMS, LAST_GROSS_COMPOSITION
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
    zvs_sv_addition = amount_for_code(text, "ZVS")

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

    salary_items = parse_salary_items(text)
    LAST_SURCHARGE_ITEMS = [i for i in salary_items if i.get("category")]
    LAST_WAGE_ITEMS = [i for i in salary_items if i.get("wage_item_type") and not i.get("category")]
    LAST_GROSS_COMPOSITION = build_gross_composition(salary_items, gross, tax_gross, sv_gross, zvs_sv_addition)
    LAST_PAYROLL_DETAILS = {
        "kv_gross": kv_gross,
        "rv_gross": rv_gross,
        "health_base": health_base,
        "health_additional": health_additional,
        "inferred_health_additional_rate": inferred_health_additional_rate,
        "care_base": care_base,
        "care_childless": care_childless,
        "inferred_care_rate": inferred_care_rate,
        "supplementary_pension": supplementary,
        "gwv_deduction": gwv,
        "payout_correction": payout_correction,
        "zvs_sv_addition": zvs_sv_addition,
    }

    detected = sum(1 for value in fields.values() if value not in (None, "", 0.0))
    return {
        "fields": fields,
        "salary_items": salary_items,
        "surcharge_items": LAST_SURCHARGE_ITEMS,
        "wage_items": LAST_WAGE_ITEMS,
        "gross_composition": LAST_GROSS_COMPOSITION,
        "tax_context": LAST_TAX_CONTEXT.copy(),
        "details": LAST_PAYROLL_DETAILS.copy(),
        "detected_fields": detected,
    }
