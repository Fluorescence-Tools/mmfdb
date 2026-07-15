"""Optical-component curation surface for the MMFDB web-admin.

This is the browser port of ChiSurf's Qt ``OpticalComponentDock``: a
component-type switcher, a curation toolbar (import / approve / reject / review
queue / AI triage / find duplicates), a filterable checkbox table with status
colouring, an overlaid spectrum plot, and a read-only detail panel — plus the
duplicate review-and-merge page.

The plot is rendered as inline SVG using presentation attributes only (no
``style`` attribute and no external assets), so it satisfies the web-admin's
strict ``default-src 'self'`` content-security policy without any JavaScript
charting dependency.

The component registry and detail-field lists mirror the ``components.json`` and
``*.view.json`` files that drive the desktop dock; they are embedded here so the
standalone package carries no ChiSurf dependency.
"""

from __future__ import annotations

import html
from typing import Any

# --- Component registry (mirrors optical_components/components.json) ----------

#: Verification states, in the order the desktop status filter lists them.
STATUS_CHOICES = ("all", "approved", "unverified", "rejected", "needs_review")

#: Emoji + CSS badge class per verification status.
STATUS_BADGE = {
    "approved": ("✅", "ok"),
    "rejected": ("❌", "bad"),
    "unverified": ("🟡", "warn"),
    "needs_review": ("🔍", "review"),
}

#: Colour palette used to distinguish overlaid probes (RGB).
_PROBE_COLORS = (
    (230, 25, 75), (60, 180, 75), (0, 130, 200), (245, 130, 48), (145, 30, 180),
    (0, 158, 158), (240, 50, 230), (170, 170, 40), (128, 0, 64), (0, 92, 128),
)

#: Colour per spectrum type when a single probe is shown (RGB).
_SPECTRUM_COLORS = {
    "absorption": (0, 100, 200), "excitation": (0, 100, 200), "emission": (200, 0, 0),
    "transmission": (0, 150, 0), "reflectance": (150, 150, 0),
    "quantum_efficiency": (150, 0, 150), "responsivity": (0, 150, 150),
}

#: Dash pattern per spectrum type so overlaid types stay distinguishable.
_SPECTRUM_DASH = {
    "absorption": "7 4", "excitation": "2 4", "emission": None,
    "transmission": None, "reflectance": "7 4 2 4",
    "quantum_efficiency": "7 4", "responsivity": "2 4",
}

_SPECTRUM_LABELS = {
    "absorption": "Absorption", "emission": "Emission", "excitation": "Excitation",
    "transmission": "Transmission", "reflectance": "Reflectance",
    "quantum_efficiency": "Quantum Efficiency", "responsivity": "Responsivity",
}

# Detail-panel fields shared by every component (mirrors the *.view.json tails).
_COMMON_TAIL = (
    ("verification_status", "Status"),
    ("source", "Source"),
    ("source_ref", "Source ref"),
    ("verified_by", "Verified by"),
    ("verified_at", "Verified at"),
    ("description", "Description"),
)
_IDENTITY_HEAD = (
    ("probe_id", "Probe ID"),
    ("chromophore_name", "Name"),
    ("category", "Category"),
    ("type_name", "Type"),
)

COMPONENT_TYPES: tuple[dict[str, Any], ...] = (
    {
        "key": "fluorophore",
        "label": "Fluorophores",
        "icon": "🌈",
        "categories": ["fluorophore", "organic_dye", "protein", "quantum_dot",
                       "nanoparticle", "other"],
        "columns": [("ID", "probe_id"), ("Name", "chromophore_name"), ("Type", "type_name"),
                    ("Abs max", "abs_max"), ("Em max", "em_max"), ("QY", "qy"),
                    ("Status", "verification_status"), ("Quality", "quality"),
                    ("Source", "source")],
        "spectra": [("absorption", "Absorption"), ("emission", "Emission")],
        "detail": _IDENTITY_HEAD + (
            ("abs_max", "Abs max"), ("em_max", "Em max"), ("qy", "QY"),
            ("ext_coeff", "Extinction"), ("lifetime", "Lifetime"),
        ) + _COMMON_TAIL[:1] + (("quality", "Quality"),) + _COMMON_TAIL[1:],
    },
    {
        "key": "filter",
        "label": "Filters",
        "icon": "🛡️",
        "categories": ["filter"],
        "columns": [("ID", "probe_id"), ("Name", "chromophore_name"), ("Type", "type_name"),
                    ("Cut-On (nm)", "cut_on"), ("Cut-Off (nm)", "cut_off"),
                    ("Center (nm)", "center_wavelength"), ("Bandwidth (nm)", "bandwidth"),
                    ("Status", "verification_status"), ("Source", "source")],
        "spectra": [("transmission", "Transmission")],
        "detail": _IDENTITY_HEAD + (
            ("cut_on", "Cut-On (nm)"), ("cut_off", "Cut-Off (nm)"),
            ("center_wavelength", "Center Wavelength (nm)"), ("bandwidth", "Bandwidth (nm)"),
            ("optical_density", "Optical Density"),
        ) + _COMMON_TAIL,
    },
    {
        "key": "dichroic",
        "label": "Dichroics",
        "icon": "🪞",
        "categories": ["dichroic"],
        "columns": [("ID", "probe_id"), ("Name", "chromophore_name"), ("Type", "type_name"),
                    ("Status", "verification_status"), ("Source", "source")],
        "spectra": [("transmission", "Transmission"), ("reflectance", "Reflectance")],
        "detail": _IDENTITY_HEAD + _COMMON_TAIL,
    },
    {
        "key": "detector",
        "label": "Detectors",
        "icon": "👁️",
        "categories": ["detector"],
        "columns": [("ID", "probe_id"), ("Name", "chromophore_name"), ("Type", "type_name"),
                    ("Status", "verification_status"), ("Source", "source")],
        "spectra": [("quantum_efficiency", "Quantum Efficiency"), ("responsivity", "Responsivity")],
        "detail": _IDENTITY_HEAD + _COMMON_TAIL,
    },
    {
        "key": "light_source",
        "label": "Light Sources",
        "icon": "💡",
        "categories": ["light_source"],
        "columns": [("ID", "probe_id"), ("Name", "chromophore_name"), ("Type", "type_name"),
                    ("Status", "verification_status"), ("Source", "source")],
        "spectra": [("emission", "Emission")],
        "detail": _IDENTITY_HEAD + _COMMON_TAIL,
    },
)

COMPONENT_BY_KEY = {item["key"]: item for item in COMPONENT_TYPES}
DEFAULT_COMPONENT_KEY = COMPONENT_TYPES[0]["key"]

# optical_properties.property_name spellings that back a detail-panel attribute.
_PROPERTY_ALIASES = {
    "abs_max": ("abs_max", "Absorption max wavelength (nm)"),
    "em_max": ("em_max", "Emission max wavelength (nm)"),
    "qy": ("qy",),
    "ext_coeff": ("ext_coeff",),
    "lifetime": ("lifetime",),
    "cut_on": ("cut_on", "Cut-On Wavelength (nm)"),
    "cut_off": ("cut_off", "Cut-Off Wavelength (nm)"),
    "center_wavelength": ("center_wavelength", "Center Wavelength (nm)"),
    "bandwidth": ("bandwidth", "Bandwidth (nm)"),
    "optical_density": ("optical_density", "Optical Density"),
}


def _esc(value: Any) -> str:
    """HTML-escape a scalar for text/attribute contexts (``None`` → empty)."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def component_for(key: str | None) -> dict[str, Any]:
    """Return the registry entry for *key*, falling back to the default type."""
    return COMPONENT_BY_KEY.get(key or "", COMPONENT_BY_KEY[DEFAULT_COMPONENT_KEY])


def merge_detail(probe: dict[str, Any], optical_properties: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold ``optical_properties`` rows into a flat attribute dict for the panel."""
    merged = dict(probe)
    by_name = {row.get("property_name"): row.get("property_value") for row in optical_properties}
    for attr, aliases in _PROPERTY_ALIASES.items():
        if merged.get(attr) in (None, ""):
            for alias in aliases:
                if by_name.get(alias) not in (None, ""):
                    merged[attr] = by_name[alias]
                    break
    return merged


# --- Inline-SVG spectrum plot ------------------------------------------------


def _normalize(values: list[float]) -> list[float]:
    peak = max((v for v in values if v is not None), default=0.0)
    if peak <= 0:
        return [0.0 for _ in values]
    return [(v or 0.0) / peak for v in values]


def _collect_traces(details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten probe detail dicts into normalized, styled plot traces."""
    single = len(details) == 1
    traces: list[dict[str, Any]] = []
    for p_idx, detail in enumerate(details):
        probe = detail.get("probe", {})
        name = probe.get("chromophore_name") or f"Probe {probe.get('probe_id', p_idx + 1)}"
        for spec in detail.get("spectra", []):
            stype = spec.get("spectrum_type", "")
            wl = [float(w) for w in spec.get("wavelengths", []) if w is not None]
            iv = spec.get("intensity", spec.get("intensity_values", []))
            iv = [float(v) for v in iv if v is not None]
            if len(wl) < 2 or len(iv) < 2:
                continue
            count = min(len(wl), len(iv))
            color = _SPECTRUM_COLORS.get(stype, (100, 100, 100)) if single \
                else _PROBE_COLORS[p_idx % len(_PROBE_COLORS)]
            label = _SPECTRUM_LABELS.get(stype, stype.replace("_", " ").title())
            traces.append({
                "name": label if single else f"{name} · {label}",
                "x": wl[:count],
                "y": _normalize(iv[:count]),
                "color": color,
                "dash": _SPECTRUM_DASH.get(stype),
            })
    return traces


def build_spectrum_svg(details: list[dict[str, Any]]) -> str:
    """Render an overlaid, normalized spectrum plot as an inline SVG string."""
    width, height = 940, 360
    left, right, top, bottom = 54, 200, 18, 44
    plot_w = width - left - right
    plot_h = height - top - bottom
    traces = _collect_traces(details)

    if not traces:
        return (
            f'<svg viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="No spectra" class="spectrum-svg" preserveAspectRatio="xMidYMid meet">'
            f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" '
            f'fill="#0e2c24" fill-opacity="0.04"/>'
            f'<text x="{width / 2:.0f}" y="{height / 2:.0f}" text-anchor="middle" '
            f'fill="#64726d" font-size="15">📉 No spectra for this selection</text></svg>'
        )

    xs = [x for tr in traces for x in tr["x"]]
    x_min, x_max = min(xs), max(xs)
    if x_max - x_min < 1:
        x_max = x_min + 1

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * plot_w

    def sy(value: float) -> float:
        return top + (1 - max(0.0, min(1.0, value))) * plot_h

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Spectra" '
        f'class="spectrum-svg" preserveAspectRatio="xMidYMid meet">',
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#ffffff"/>',
    ]

    # Horizontal grid + y ticks (normalized 0..1).
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = sy(frac)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="#e2e8e5" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" fill="#8a988f" '
            f'font-size="11">{frac:.2f}</text>'
        )
    # Vertical grid + x ticks (~6 across the wavelength range).
    for i in range(6):
        value = x_min + (x_max - x_min) * i / 5
        x = sx(value)
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" '
            f'stroke="#eef2f0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{top + plot_h + 18}" text-anchor="middle" '
            f'fill="#8a988f" font-size="11">{value:.0f}</text>'
        )
    parts.append(
        f'<text x="{left + plot_w / 2:.0f}" y="{height - 6}" text-anchor="middle" '
        f'fill="#64726d" font-size="12">Wavelength (nm)</text>'
    )
    parts.append(
        f'<text x="14" y="{top + plot_h / 2:.0f}" text-anchor="middle" fill="#64726d" '
        f'font-size="12" transform="rotate(-90 14 {top + plot_h / 2:.0f})">Normalized</text>'
    )

    # Traces.
    for tr in traces:
        r, g, b = tr["color"]
        stroke = f"rgb({r},{g},{b})"
        points = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(tr["x"], tr["y"]))
        dash = f' stroke-dasharray="{tr["dash"]}"' if tr["dash"] else ""
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{stroke}" '
            f'stroke-width="2" stroke-linejoin="round"{dash}/>'
        )

    # Legend.
    legend_x = left + plot_w + 18
    for idx, tr in enumerate(traces[:12]):
        r, g, b = tr["color"]
        y = top + 8 + idx * 20
        dash = ' stroke-dasharray="7 4"' if tr["dash"] else ""
        parts.append(
            f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 22}" y2="{y}" '
            f'stroke="rgb({r},{g},{b})" stroke-width="3"{dash}/>'
        )
        parts.append(
            f'<text x="{legend_x + 30}" y="{y + 4}" fill="#3a4742" font-size="11">'
            f'{_esc(tr["name"][:34])}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# --- HTML fragments ----------------------------------------------------------


def _query(ct: str, status: str, search: str, sel: list[int], probe: int | None = None) -> str:
    """Build an escaped ``/optical-components`` query string from view state."""
    from urllib.parse import urlencode
    params: list[tuple[str, str]] = [("ct", ct)]
    if status and status != "all":
        params.append(("status", status))
    if search:
        params.append(("search", search))
    for pid in sel:
        params.append(("sel", str(pid)))
    if probe is not None:
        params.append(("probe", str(probe)))
    return _esc("?" + urlencode(params)) if params else ""


def render_tabs(active_key: str) -> str:
    """Component-type switcher (mirrors the desktop radio row)."""
    links = "".join(
        f'<a class="ct-tab{" active" if item["key"] == active_key else ""}" '
        f'href="/optical-components?ct={item["key"]}">{item["icon"]} {_esc(item["label"])}</a>'
        for item in COMPONENT_TYPES
    )
    return f'<div class="ct-tabs">{links}</div>'


def render_toolbar(ct: str, status: str, search: str, sel: list[int], csrf: str) -> str:
    """Curation toolbar: import / approve / reject / review queue / triage / duplicates."""
    hidden = (
        f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        f'<input type="hidden" name="ct" value="{_esc(ct)}">'
        f'<input type="hidden" name="status" value="{_esc(status)}">'
        f'<input type="hidden" name="search" value="{_esc(search)}">'
        + "".join(f'<input type="hidden" name="sel" value="{pid}">' for pid in sel)
    )
    sel_note = f"{len(sel)} checked" if sel else "none checked"
    review_href = f"/optical-components?ct={_esc(ct)}&amp;status=unverified"
    dup_href = f"/optical-components/duplicates?ct={_esc(ct)}"
    return (
        '<div class="toolbar">'
        f'<form method="post" action="/optical-components/action" class="inline">{hidden}'
        '<button name="op" value="defaults" class="tool" title="Load ChiSurf built-in default fluorophore spectra">🧪 Load defaults</button>'
        '<button name="op" value="import" class="tool" title="Import bundled reference set">📥 Import ref set</button>'
        f'<button name="op" value="approve" class="tool"{"" if sel else " disabled"} '
        'title="Approve checked items">✅ Approve</button>'
        f'<button name="op" value="reject" class="tool"{"" if sel else " disabled"} '
        'title="Reject checked items">❌ Reject</button>'
        f'<button name="op" value="triage" class="tool"{"" if sel else " disabled"} '
        'title="Run AI triage on checked items">🤖 AI triage</button>'
        '</form>'
        f'<a class="tool" href="{review_href}" title="Show items needing review">🔍 Review queue</a>'
        f'<a class="tool" href="{dup_href}" title="Find and merge duplicates">👯 Find duplicates</a>'
        f'<span class="tool-note">🧬 {sel_note}</span>'
        '</div>'
    )


def render_filter_bar(ct: str, status: str, search: str) -> str:
    """Search + status filter (GET) mirroring the desktop filter row."""
    options = "".join(
        f'<option value="{s}"{" selected" if s == status else ""}>{s}</option>'
        for s in STATUS_CHOICES
    )
    return (
        '<form method="get" action="/optical-components" class="filter-bar">'
        f'<input type="hidden" name="ct" value="{_esc(ct)}">'
        f'<input name="search" value="{_esc(search)}" placeholder="🔎 Search name…" maxlength="128">'
        f'<label class="filter-status">Status<select name="status">{options}</select></label>'
        '<button type="submit" class="tool">🔄 Refresh</button>'
        '</form>'
    )


def render_table(component: dict[str, Any], rows: list[dict[str, Any]], *,
                 ct: str, status: str, search: str, sel: list[int],
                 probe: int | None, csrf: str) -> str:
    """Checkbox table + the "plot checked" submit, coloured by status."""
    sel_set = set(sel)
    headers = "".join(f"<th>{_esc(label)}</th>" for label, _ in component["columns"])
    body: list[str] = []
    for row in rows:
        pid = row.get("probe_id")
        checked = " checked" if pid in sel_set else ""
        cells = []
        for _, key in component["columns"]:
            value = row.get(key)
            if key == "verification_status":
                emoji, badge = STATUS_BADGE.get(str(value), ("", "warn"))
                cells.append(f'<td><span class="badge {badge}">{emoji} {_esc(value)}</span></td>')
            elif key == "probe_id":
                href = _query(ct, status, search, sel, probe=pid)
                cells.append(
                    f'<td><a class="row-open" href="/optical-components{href}" '
                    f'title="Show details + spectrum">{_esc(value)}</a></td>'
                )
            else:
                cells.append(f"<td>{_esc(value)}</td>")
        active = ' class="row-active"' if pid == probe else ""
        body.append(
            f'<tr{active}><td class="pick"><input type="checkbox" name="sel" value="{pid}"{checked}></td>'
            + "".join(cells) + "</tr>"
        )
    empty_cols = len(component["columns"]) + 1
    tbody = "".join(body) or f'<tr><td colspan="{empty_cols}" class="empty">No records</td></tr>'
    return (
        '<form method="get" action="/optical-components" class="ct-table-form">'
        f'<input type="hidden" name="ct" value="{_esc(ct)}">'
        f'<input type="hidden" name="status" value="{_esc(status)}">'
        f'<input type="hidden" name="search" value="{_esc(search)}">'
        + (f'<input type="hidden" name="probe" value="{probe}">' if probe is not None else "")
        + '<div class="table-actions">'
        '<button type="submit" class="tool">📈 Plot checked</button>'
        f'<a class="tool ghost" href="/optical-components?ct={_esc(ct)}">🧹 Clear</a>'
        '</div>'
        '<div class="table-wrap"><table class="ct-table"><thead><tr>'
        f'<th class="pick">☑</th>{headers}</tr></thead><tbody>{tbody}</tbody></table></div>'
        '</form>'
    )


def render_detail(component: dict[str, Any], merged: dict[str, Any] | None) -> str:
    """Read-only property panel for the focused probe."""
    if not merged:
        return (
            '<section class="detail-panel"><h2>🔬 Details</h2>'
            '<p class="empty">Select a probe ID to inspect its properties.</p></section>'
        )
    rows = "".join(
        f'<dt>{_esc(label)}</dt><dd>{_esc(merged.get(attr))}</dd>'
        for attr, label in component["detail"]
    )
    name = merged.get("chromophore_name") or merged.get("probe_id")
    return (
        f'<section class="detail-panel"><h2>🔬 {_esc(name)}</h2>'
        f'<dl class="detail-grid">{rows}</dl></section>'
    )


def render_page(*, component: dict[str, Any], rows: list[dict[str, Any]], total: int,
                status: str, search: str, sel: list[int], probe: int | None,
                spectra_details: list[dict[str, Any]], detail: dict[str, Any] | None,
                message: str, csrf: str) -> str:
    """Assemble the full optical-components page body."""
    ct = component["key"]
    svg = build_spectrum_svg(spectra_details)
    notice = f'<p class="notice">{_esc(message)}</p>' if message else ""
    return (
        '<div class="page-head"><div><p class="eyebrow">Optical components</p>'
        f'<h1>{component["icon"]} {_esc(component["label"])}</h1>'
        f'<p>{len(rows)} shown · {total} total</p></div></div>'
        + render_tabs(ct)
        + render_toolbar(ct, status, search, sel, csrf)
        + render_filter_bar(ct, status, search)
        + notice
        + f'<section class="plot-card"><h2>📊 Spectra</h2>{svg}</section>'
        + '<div class="ct-split">'
        + render_table(component, rows, ct=ct, status=status, search=search,
                       sel=sel, probe=probe, csrf=csrf)
        + render_detail(component, detail)
        + '</div>'
    )


def render_duplicates_page(*, ct: str, groups: list[dict[str, Any]],
                           spectra_by_group: dict[int, list[dict[str, Any]]],
                           message: str, csrf: str) -> str:
    """Render the duplicate review-and-merge page (one form, radio primary + checkbox group)."""
    component = component_for(ct)
    notice = f'<p class="notice">{_esc(message)}</p>' if message else ""
    if not groups:
        return (
            '<div class="page-head"><div><p class="eyebrow">Optical components</p>'
            '<h1>👯 Duplicates</h1></div></div>' + notice
            + f'<a class="tool" href="/optical-components?ct={_esc(ct)}">← Back to {_esc(component["label"])}</a>'
            + '<section><p class="empty">🎉 No duplicate groups found.</p></section>'
        )

    blocks: list[str] = []
    for g_idx, group in enumerate(groups):
        probes = group["probes"]
        longest = max(probes, key=lambda p: len(p.get("chromophore_name") or ""))
        rows = []
        for probe in probes:
            pid = probe["probe_id"]
            emoji, badge = STATUS_BADGE.get(str(probe.get("verification_status")), ("", "warn"))
            primary = " checked" if pid == longest["probe_id"] else ""
            rows.append(
                f'<tr><td class="pick"><input type="radio" name="primary_{g_idx}" '
                f'value="{pid}"{primary}></td>'
                f'<td>{pid}</td><td>{_esc(probe.get("chromophore_name"))}</td>'
                f'<td>{_esc(probe.get("category"))}</td><td>{_esc(probe.get("source"))}</td>'
                f'<td><span class="badge {badge}">{emoji} {_esc(probe.get("verification_status"))}</span></td></tr>'
            )
        svg = build_spectrum_svg(spectra_by_group.get(g_idx, []))
        blocks.append(
            '<section class="dup-group">'
            f'<label class="dup-head"><input type="checkbox" name="merge" value="{g_idx}"> '
            f'<strong>{_esc(group["norm_name"])}</strong> · {len(probes)} candidates</label>'
            f'<input type="hidden" name="ids_{g_idx}" value="{",".join(str(p["probe_id"]) for p in probes)}">'
            '<div class="dup-body">'
            '<div class="table-wrap"><table class="ct-table"><thead><tr>'
            '<th class="pick">Primary</th><th>ID</th><th>Name</th><th>Category</th>'
            '<th>Source</th><th>Status</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
            f'<div class="plot-card">{svg}</div>'
            '</div></section>'
        )

    return (
        '<div class="page-head"><div><p class="eyebrow">Optical components</p>'
        '<h1>👯 Duplicates</h1><p>Pick a primary per group, check the groups to merge.</p></div></div>'
        + notice
        + f'<a class="tool" href="/optical-components?ct={_esc(ct)}">← Back to {_esc(component["label"])}</a>'
        + '<form method="post" action="/optical-components/duplicates">'
        + f'<input type="hidden" name="csrf" value="{_esc(csrf)}">'
        + f'<input type="hidden" name="ct" value="{_esc(ct)}">'
        + f'<input type="hidden" name="group_count" value="{len(groups)}">'
        + "".join(blocks)
        + '<div class="table-actions"><button type="submit" class="tool">🔗 Merge checked groups</button></div>'
        + '</form>'
    )
