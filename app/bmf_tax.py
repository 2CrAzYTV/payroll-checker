from __future__ import annotations

from typing import Any

from taxpy.big import Big
from taxpy.Lohnsteuer2026Big import Lohnsteuer2026Big


def _eur_from_cent(value: Any) -> float:
    if hasattr(value, "to_number"):
        return round(float(value.to_number()) / 100.0, 2)
    return round(float(value) / 100.0, 2)


def calculate_bmf_2026(
    *,
    monthly_tax_gross: float,
    tax_class: int = 1,
    kv_additional_rate: float = 2.9,
    child_allowance: float = 0.0,
    childless_care_surcharge: bool = False,
    saxony: bool = False,
    church_tax_rate: float = 0.0,
) -> dict[str, Any]:
    """Calculate monthly wage tax using the BMF PAP 2026 implementation.

    RE4 is supplied in cents for a monthly pay period (LZZ=2). The calculation
    stays local in the container; no BMF web service is called at runtime.
    """
    if tax_class not in {1, 2, 3, 4, 5, 6}:
        raise ValueError("Steuerklasse muss zwischen 1 und 6 liegen")

    tax = Lohnsteuer2026Big()
    tax.LZZ = 2
    tax.RE4 = Big(round(monthly_tax_gross * 100))
    tax.STKL = tax_class
    tax.KVZ = Big(str(kv_additional_rate))
    tax.ZKF = Big(str(child_allowance))
    tax.PVZ = 1 if childless_care_surcharge else 0
    tax.PVS = 1 if saxony else 0
    tax.PKV = 0
    tax.KRV = 0
    tax.ALV = 0
    tax.calculate()

    wage_tax = _eur_from_cent(tax.LSTLZZ)
    solidarity = _eur_from_cent(tax.SOLZLZZ)
    church_base = _eur_from_cent(tax.BK)
    church_tax = round(church_base * church_tax_rate / 100.0, 2) if church_tax_rate else 0.0

    return {
        "year": 2026,
        "pay_period": "monthly",
        "tax_gross": round(monthly_tax_gross, 2),
        "tax_class": tax_class,
        "kv_additional_rate": round(kv_additional_rate, 4),
        "child_allowance": child_allowance,
        "childless_care_surcharge": childless_care_surcharge,
        "saxony": saxony,
        "wage_tax": wage_tax,
        "solidarity_surcharge": solidarity,
        "church_tax_base": church_base,
        "church_tax": church_tax,
        "source": "BMF PAP 2026, Stand 12.11.2025",
    }
