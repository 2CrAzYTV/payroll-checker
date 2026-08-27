from __future__ import annotations

import uvicorn

from app import main, payroll_parser
from app.bmf_tax import calculate_bmf_2026

# Runtime extensions for the current parser/tax engine while keeping the existing API/database stable.
main.parse_payroll_text = payroll_parser.parse_payroll_text
_base_perform_check = main.perform_check


def perform_check_with_bmf(data):
    result = _base_perform_check(data)

    if data.tax_gross is not None and data.tax_gross > 0:
        ctx = payroll_parser.LAST_TAX_CONTEXT.copy()
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

        result["bmf_tax"] = bmf
        result["notice"] = (
            "Die Lohnsteuer und der Solidaritätszuschlag werden lokal nach dem BMF-Programmablaufplan 2026 "
            "(endgültiger Stand 12.11.2025) neu berechnet. Zusätzlich werden Sozialversicherung und "
            "Auszahlungskette geprüft. Bei Sonderzahlungen, Mehrfachbeschäftigung oder nicht erkannten "
            "ELStAM-Merkmalen ist eine manuelle Kontrolle weiterhin erforderlich."
        )

    result["version"] = "0.3.0"
    return result


main.perform_check = perform_check_with_bmf
main.APP_VERSION = "0.3.0"
main.app.version = "0.3.0"


if __name__ == "__main__":
    uvicorn.run(main.app, host="0.0.0.0", port=8788, access_log=False)
