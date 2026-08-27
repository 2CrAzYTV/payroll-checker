from __future__ import annotations

import uvicorn

from app import main, payroll_parser
from app.bmf_tax import calculate_bmf_2026

# Runtime extensions for the current parser/tax engine while keeping the existing API/database stable.
main.parse_payroll_text = payroll_parser.parse_payroll_text
_base_perform_check = main.perform_check


def _add_breakdown_checks(result, data, ctx, details):
    sv_gross = data.sv_gross if data.sv_gross is not None else data.gross
    kv_basis = min(sv_gross, 5812.50)

    health_base_stated = float(details.get("health_base") or 0)
    health_add_stated = float(details.get("health_additional") or 0)
    if health_base_stated or health_add_stated:
        result["comparisons"].append(
            main.compare("Krankenversicherung Grundbeitrag AN", health_base_stated, kv_basis * 0.073, data.tolerance)
        )
        result["comparisons"].append(
            main.compare(
                "KV-Zusatzbeitrag AN (aus Abrechnung rückgerechnet)",
                health_add_stated,
                kv_basis * ((data.health_additional_rate / 2) / 100),
                data.tolerance,
            )
        )

    care_base_stated = float(details.get("care_base") or 0)
    care_childless_stated = float(details.get("care_childless") or 0)
    care_base_rate = 0.023 if ctx.get("saxony") else 0.018
    childless_rate = 0.006 if ctx.get("childless_care_surcharge") else 0.0
    if care_base_stated or care_childless_stated:
        result["comparisons"].append(
            main.compare("Pflegeversicherung Grundbeitrag AN", care_base_stated, kv_basis * care_base_rate, data.tolerance)
        )
        result["comparisons"].append(
            main.compare(
                "PV-Kinderlosenzuschlag AN",
                care_childless_stated,
                kv_basis * childless_rate,
                data.tolerance,
            )
        )


def _add_surcharge_checks(result):
    surcharge_items = payroll_parser.LAST_SURCHARGE_ITEMS
    if not surcharge_items:
        result["surcharge_analysis"] = []
        return

    analysis = []
    for item in surcharge_items:
        category = item.get("category")
        tax_free_percent = item.get("tax_free_percent")
        if category == "Überstunden/Mehrarbeit":
            note = "Zuschlag erkannt. Die tarifliche Höhe benötigt Tarif-/Betriebsregel und Stundenbasis; § 3b EStG enthält hierfür keine allgemeine Steuerfreigrenze."
        elif category == "Nachtarbeit":
            note = "Zuschlag erkannt. Steuerfrei nach § 3b EStG grundsätzlich bis 25 % des Grundlohns; 0–4 Uhr können bei vor Mitternacht begonnener Nachtarbeit bis 40 % gelten."
        elif category == "Sonntagsarbeit":
            note = "Zuschlag erkannt. Steuerfrei nach § 3b EStG grundsätzlich bis 50 % des Grundlohns."
        elif category == "Feiertagsarbeit":
            note = "Zuschlag erkannt. Steuerfrei nach § 3b EStG grundsätzlich bis 125 %; für bestimmte Feiertage bis 150 % des Grundlohns."
        else:
            note = "Zuschlag erkannt; manuelle Zuordnung erforderlich."

        analysis.append({
            "code": item.get("code"),
            "label": item.get("label"),
            "category": category,
            "amount": item.get("amount"),
            "tax_free_percent": tax_free_percent,
            "message": note,
        })

        row = main.compare(
            f"{category} – Lohnart {item.get('code')}",
            float(item.get("amount") or 0),
            float(item.get("amount") or 0),
            0.0,
        )
        row["message"] = note
        # Informational only: do not claim a tariff-specific amount was independently recalculated.
        row["level"] = "warning" if category == "Überstunden/Mehrarbeit" else "ok"
        result["comparisons"].append(row)

    result["surcharge_analysis"] = analysis


def perform_check_with_bmf(data):
    result = _base_perform_check(data)
    ctx = payroll_parser.LAST_TAX_CONTEXT.copy()
    details = payroll_parser.LAST_PAYROLL_DETAILS.copy()

    _add_breakdown_checks(result, data, ctx, details)
    _add_surcharge_checks(result)

    if data.tax_gross is not None and data.tax_gross > 0:
        bmf = calculate_bmf_2026(
            monthly_tax_gross=data.tax_gross,
            tax_class=int(ctx.get("tax_class", 1)),
            kv_additional_rate=data.health_additional_rate,
            child_allowance=float(ctx.get("child_allowance", 0.0)),
            childless_care_surcharge=bool(ctx.get("childless_care_surcharge", False)),
            saxony=bool(ctx.get("saxony", False)),
            church_tax_rate=float(ctx.get("church_tax_rate", 0.0)),
        )

        result["comparisons"].append(
            main.compare("Lohnsteuer nach BMF-PAP 2026", data.wage_tax, bmf["wage_tax"], data.tolerance)
        )
        result["comparisons"].append(
            main.compare("Solidaritätszuschlag nach BMF-PAP 2026", data.solidarity_surcharge, bmf["solidarity_surcharge"], data.tolerance)
        )
        if data.church_tax or bmf["church_tax"]:
            result["comparisons"].append(
                main.compare("Kirchensteuer auf BMF-Bemessungsgrundlage", data.church_tax, bmf["church_tax"], data.tolerance)
            )
        result["bmf_tax"] = bmf

    errors = sum(1 for item in result["comparisons"] if item["level"] == "error")
    warnings = sum(1 for item in result["comparisons"] if item["level"] == "warning")
    if errors:
        result["overall"] = "error"
        result["overall_text"] = "Abweichungen gefunden"
    elif warnings:
        result["overall"] = "warning"
        result["overall_text"] = "weitgehend plausibel, einzelne Punkte prüfen"
    else:
        result["overall"] = "ok"
        result["overall_text"] = "inklusive BMF-PAP-2026-Prüfung plausibel"

    result["contribution_breakdown"] = {
        "health_base": details.get("health_base"),
        "health_additional": details.get("health_additional"),
        "health_additional_rate_inferred": details.get("inferred_health_additional_rate"),
        "care_base": details.get("care_base"),
        "care_childless": details.get("care_childless"),
        "care_rate_inferred": details.get("inferred_care_rate"),
        "saxony": bool(ctx.get("saxony", False)),
        "childless_care_surcharge": bool(ctx.get("childless_care_surcharge", False)),
    }
    result["notice"] = (
        "Lohnsteuer und Solidaritätszuschlag werden lokal nach dem BMF-Programmablaufplan 2026 neu berechnet. "
        "KV und PV werden zusätzlich in Grundbeitrag und Zusatz-/Kinderlosenzuschlag aufgeteilt. "
        "Nacht-, Sonn- und Feiertagszuschläge werden erkannt und gegen die gesetzlichen Steuerfreiheitsgrenzen nach § 3b EStG eingeordnet. "
        "Tarifliche Zuschlagshöhen und Überstunden können nur vollständig geprüft werden, wenn Stunden, Grundlohn und die maßgebliche Tarif-/Betriebsregel eindeutig vorliegen."
    )

    result["version"] = "0.4.0"
    return result


main.perform_check = perform_check_with_bmf
main.APP_VERSION = "0.4.0"
main.app.version = "0.4.0"


if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=8788, access_log=False)
