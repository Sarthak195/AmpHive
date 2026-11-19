"""
Driver/CPO insights helpers (F8): the estimate assumptions and the pure maths.

Every environmental or savings figure derived here is an ESTIMATE built from
the env-tunable assumptions below, and the API always ships those assumptions
beside the numbers so the UI can show the factor it used. Nothing here touches
the database.

Defaults describe a small electric two- or three-wheeler or compact car charged
on the Indian grid:
- GRID_EMISSION_FACTOR_KG_PER_KWH 0.71: CEA CO2 Baseline Database (India).
- PETROL_CO2_KG_PER_L 2.31: tailpipe CO2 per litre of petrol burned.
- ICE_KM_PER_L 15 and EV_KM_PER_KWH 6: the petrol vehicle replaced and the EV's
  efficiency. Distance is derived from the energy delivered, not measured.
- PETROL_PRICE_PER_L 100 (INR): retail petrol price used for the equivalent.
"""
import os
from datetime import datetime
from typing import Any


def _float_env(name: str, default: float) -> float:
    """A positive float from the environment; a missing, malformed or
    non-positive value falls back to the default (never crash on import)."""
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


GRID_EMISSION_FACTOR_KG_PER_KWH = _float_env("GRID_EMISSION_FACTOR_KG_PER_KWH", 0.71)
PETROL_CO2_KG_PER_L = _float_env("PETROL_CO2_KG_PER_L", 2.31)
PETROL_PRICE_PER_L = _float_env("PETROL_PRICE_PER_L", 100.0)
ICE_KM_PER_L = _float_env("ICE_KM_PER_L", 15.0)
EV_KM_PER_KWH = _float_env("EV_KM_PER_KWH", 6.0)

SERIES_MONTHS = 12


def co2_avoided_kg_per_kwh() -> float:
    """Net CO2 avoided per kWh: the petrol CO2 for the distance that kWh
    drives, minus the grid CO2 of generating that kWh. Can be negative if the
    env assumptions are set so."""
    petrol_kg = EV_KM_PER_KWH / ICE_KM_PER_L * PETROL_CO2_KG_PER_L
    return petrol_kg - GRID_EMISSION_FACTOR_KG_PER_KWH


def assumptions() -> dict[str, Any]:
    """The factors behind every estimate, shipped beside the figures."""
    return {
        "estimate": True,
        "grid_emission_factor_kg_per_kwh": GRID_EMISSION_FACTOR_KG_PER_KWH,
        "petrol_co2_kg_per_l": PETROL_CO2_KG_PER_L,
        "petrol_price_per_l": PETROL_PRICE_PER_L,
        "ice_km_per_l": ICE_KM_PER_L,
        "ev_km_per_kwh": EV_KM_PER_KWH,
        "co2_avoided_kg_per_kwh": round(co2_avoided_kg_per_kwh(), 4),
    }


def estimate_impact(energy_kwh: float) -> dict[str, float]:
    """Estimated impact of `energy_kwh` delivered: distance driven, grid CO2
    emitted, petrol CO2 not burned, net CO2 avoided, petrol litres and their
    cost in INR. All values are estimates."""
    kwh = max(float(energy_kwh or 0), 0.0)
    distance_km = kwh * EV_KM_PER_KWH
    petrol_l = distance_km / ICE_KM_PER_L
    grid_kg = kwh * GRID_EMISSION_FACTOR_KG_PER_KWH
    petrol_kg = petrol_l * PETROL_CO2_KG_PER_L
    return {
        "distance_km": round(distance_km, 1),
        "grid_co2_kg": round(grid_kg, 2),
        "petrol_co2_kg": round(petrol_kg, 2),
        "co2_avoided_kg": round(petrol_kg - grid_kg, 2),
        "petrol_litres": round(petrol_l, 2),
        "petrol_equivalent_inr": round(petrol_l * PETROL_PRICE_PER_L, 2),
    }


def month_keys(now: datetime, count: int = SERIES_MONTHS) -> list[str]:
    """The last `count` calendar months ending at `now`'s month, oldest first,
    as "YYYY-MM"."""
    year, month = now.year, now.month
    keys = []
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return keys[::-1]
