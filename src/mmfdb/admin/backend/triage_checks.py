"""Deterministic sanity checks for fluorophore curation triage.

This is the standalone default checker used by the MMFDB web-admin (and any
other host that does not inject its own). It mirrors the numeric checks that
ChiSurf provides through ``deterministic_checks`` so the standalone deployment
has feature parity without importing ChiSurf.

The single public entrypoint :func:`run_deterministic_checks` takes the probe
dict assembled by :func:`mmfdb.admin.backend.fluorophore_services.handle_run_ai_triage`
and returns ``{"issues": [...], "proposed_quality": "low"|"medium"|"high"}``.
"""

from __future__ import annotations

from typing import Any


def _safe_float(value: object) -> float | None:
    """Parse a possibly-messy scalar into a float, or ``None`` when unparseable."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _check_qy(qy: float | None) -> list[str]:
    """Flag a missing or physically impossible fluorescence quantum yield."""
    issues: list[str] = []
    if qy is None:
        issues.append("missing QY")
    elif qy < 0:
        issues.append(f"negative QY: {qy}")
    elif qy > 1.0:
        issues.append(f"QY > 1: {qy}")
    return issues


def _check_ext_coeff(ec: float | None) -> list[str]:
    """Flag a missing or implausible molar extinction coefficient."""
    issues: list[str] = []
    if ec is None:
        issues.append("missing extinction coefficient")
    elif ec <= 0:
        issues.append(f"non-positive extinction coefficient: {ec}")
    elif ec > 1_000_000:
        issues.append(f"implausibly high extinction coefficient: {ec}")
    return issues


def _check_stokes_shift(abs_max: float | None, em_max: float | None) -> list[str]:
    """Flag a negative Stokes shift (emission bluer than absorption)."""
    issues: list[str] = []
    if abs_max is not None and em_max is not None and em_max < abs_max:
        issues.append(f"negative Stokes shift: em_max={em_max} < abs_max={abs_max}")
    return issues


def _check_wavelength_range(abs_max: float | None, em_max: float | None) -> list[str]:
    """Flag absorption/emission maxima outside the plausible optical range."""
    issues: list[str] = []
    for label, val in (("abs_max", abs_max), ("em_max", em_max)):
        if val is not None and (val < 200 or val > 1000):
            issues.append(f"{label}={val} outside [200, 1000] nm")
    return issues


def run_deterministic_checks(probe: dict[str, Any]) -> dict[str, Any]:
    """Run numeric sanity checks on a probe dict.

    Parameters
    ----------
    probe : dict
        Probe data with keys like ``abs_max``, ``em_max``, ``qy``,
        ``ext_coeff``, and the boolean spectrum-presence flags ``has_abs`` /
        ``has_em`` (``absorption`` / ``emission`` are accepted as aliases).

    Returns
    -------
    dict
        ``{"issues": [...], "proposed_quality": "low"|"medium"|"high"}``.

    """
    issues: list[str] = []
    abs_max = _safe_float(probe.get("abs_max"))
    em_max = _safe_float(probe.get("em_max"))
    qy = _safe_float(probe.get("qy"))
    ext_coeff = _safe_float(probe.get("ext_coeff"))

    issues.extend(_check_qy(qy))
    issues.extend(_check_ext_coeff(ext_coeff))
    issues.extend(_check_stokes_shift(abs_max, em_max))
    issues.extend(_check_wavelength_range(abs_max, em_max))

    has_abs_spec = bool(probe.get("has_abs", probe.get("absorption")))
    has_em_spec = bool(probe.get("has_em", probe.get("emission")))
    if not has_abs_spec:
        issues.append("missing absorption spectrum")
    if not has_em_spec:
        issues.append("missing emission spectrum")

    if not issues:
        proposed_quality = "high"
    elif len(issues) <= 2 and all("missing" not in issue for issue in issues):
        proposed_quality = "medium"
    else:
        proposed_quality = "low"

    return {"issues": issues, "proposed_quality": proposed_quality}
