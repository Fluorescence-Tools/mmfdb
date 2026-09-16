#!/usr/bin/env python3
"""Assemble the three panel SVGs into the single composite Figure 1.

Panels are authored independently so each can be reused on its own. They share
short class names (.bt, .bs, .mono, ...) whose definitions differ slightly, so a
naive concatenation would let one panel's CSS silently restyle another. This
script namespaces every class and marker id per panel before stacking them.

Usage: python3 build_figure1.py
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent
PANELS = ["fig1_architecture.svg", "fig2_provenance_dag.svg", "fig3_deposit_flow.svg"]
GAP = 26
OUT = HERE / "figure1.svg"


def _viewbox_height(svg: str) -> float:
    match = re.search(r'viewBox="0 0 [\d.]+ ([\d.]+)"', svg)
    if match is None:
        raise ValueError("panel is missing a 0-origin viewBox")
    return float(match.group(1))


def _namespace(svg: str, prefix: str) -> tuple[str, str, str]:
    """Return (css, defs_without_style, body) with classes and ids namespaced."""
    style = re.search(r"<style>(.*?)</style>", svg, re.S)
    css = style.group(1) if style else ""

    defs = re.search(r"<defs>(.*?)</defs>", svg, re.S)
    defs_inner = re.sub(r"<style>.*?</style>", "", defs.group(1), flags=re.S) if defs else ""

    body = svg.split("</defs>", 1)[1] if "</defs>" in svg else svg
    body = body.rsplit("</svg>", 1)[0]

    names = sorted(set(re.findall(r"\.([A-Za-z][\w-]*)\s*(?=[,{])", css)), key=len, reverse=True)
    for name in names:
        css = re.sub(rf"\.{re.escape(name)}(?=\s*[,{{])", f".{prefix}{name}", css)

    def _rewrite_class(match: re.Match[str]) -> str:
        classes = " ".join(prefix + c for c in match.group(1).split())
        return f'class="{classes}"'

    body = re.sub(r'class="([^"]*)"', _rewrite_class, body)

    for ident in sorted(set(re.findall(r'<marker id="([^"]+)"', defs_inner)), key=len, reverse=True):
        defs_inner = defs_inner.replace(f'id="{ident}"', f'id="{prefix}{ident}"')
        body = body.replace(f"url(#{ident})", f"url(#{prefix}{ident})")

    return css.strip(), defs_inner.strip(), body.strip()


def main() -> None:
    css_parts: list[str] = []
    defs_parts: list[str] = []
    body_parts: list[str] = []
    offset = 0.0

    for index, filename in enumerate(PANELS, start=1):
        svg = (HERE / filename).read_text(encoding="utf-8")
        css, defs, body = _namespace(svg, f"p{index}-")
        css_parts.append(f"/* {filename} */\n{css}")
        defs_parts.append(defs)
        body_parts.append(f'<g transform="translate(0,{offset:g})">\n{body}\n</g>')
        offset += _viewbox_height(svg) + GAP

    total = offset - GAP
    document = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1120 {total:g}" width="1120" height="{total:g}" role="img" aria-label="MMFDB: architecture, provenance graph, and deposition formats">
  <title>Figure 1 | MMFDB architecture, provenance model, and deposition</title>
  <defs>
{chr(10).join(defs_parts)}
    <style>
{chr(10).join(css_parts)}
    </style>
  </defs>
  <rect x="0" y="0" width="1120" height="{total:g}" fill="none"/>
{chr(10).join(body_parts)}
</svg>
"""
    OUT.write_text(document, encoding="utf-8")
    print(f"wrote {OUT} ({total:g} units tall, {len(PANELS)} panels)")


if __name__ == "__main__":
    main()
