"""Duplicate-probe grouping for fluorophore curation.

Pure-Python port of the grouping logic that ChiSurf's Qt duplicates dialog runs
in a worker thread, moved into MMFDB so the standalone web-admin can offer the
same "find duplicates → review → merge" workflow without any GUI dependency.

The backend ``fluorophores.find_duplicates`` RPC returns the raw probe rows (with
``optical_properties`` and ``spectra_types`` attached); :func:`group_duplicates`
turns those into candidate groups the same way the desktop client does:

* normalize names (drop filler words, canonicalize AlexaFluor / cyanine spellings),
* within each category, link probes whose normalized names match exactly or whose
  name/abs-max/em-max agree under a small Bayesian model,
* collapse the links into connected components with union-find,
* keep every component with more than one member.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

#: Posterior threshold above which two probes are treated as the same entity.
_DUPLICATE_PROBABILITY_THRESHOLD = 0.8
#: abs-max gap (nm) beyond which the sorted inner scan stops comparing.
_ABS_MAX_WINDOW_NM = 15.0


def normalize_name(name: str) -> str:
    """Return a comparison key for a probe name.

    Filler words are removed, common AlexaFluor / cyanine spellings are
    canonicalized, and everything non-alphanumeric is stripped.
    """
    text = (name or "").lower()
    for filler in ("fluor", "dye", "fluorescent"):
        text = text.replace(filler, "")
    if text.startswith("af-") or text.startswith("af "):
        text = text.replace("af", "alexa", 1)
    elif text.startswith("af") and len(text) > 2 and text[2].isdigit():
        text = "alexa" + text[2:]
    text = text.replace("cyanine", "cy")
    return re.sub(r"[^a-z0-9]", "", text)


def _duplicate_probability(p1: dict[str, Any], p2: dict[str, Any]) -> float:
    """Posterior probability that two probes describe the same entity.

    A small naive-Bayes model over name similarity plus absorption/emission
    maxima; probes from the same curated source are strongly down-weighted.
    """
    prior_dup = 0.001
    prior_not = 1 - prior_dup

    n1 = normalize_name(p1["chromophore_name"])
    n2 = normalize_name(p2["chromophore_name"])
    if n1 == n2:
        like_name_dup = 0.99
        like_name_not = 0.0001
    else:
        sim = SequenceMatcher(None, n1, n2).ratio()
        if sim < 0.6:
            like_name_dup = 1e-6
            like_name_not = 0.99
        else:
            like_name_dup = math.exp(-10 * (1 - sim) ** 2)
            like_name_not = 0.01 if sim > 0.8 else 0.1

    def _maxima_likelihoods(v1: object, v2: object) -> tuple[float, float]:
        if v1 and v2:
            try:
                diff = abs(float(v1) - float(v2))
            except (TypeError, ValueError):
                return 1.0, 1.0
            return math.exp(-0.5 * (diff / 5.0) ** 2), 1.0 / 300.0
        # A missing measurement is uninformative.
        return 1.0, 1.0

    like_abs_dup, like_abs_not = _maxima_likelihoods(p1.get("abs_max"), p2.get("abs_max"))
    like_em_dup, like_em_not = _maxima_likelihoods(p1.get("em_max"), p2.get("em_max"))

    num = prior_dup * like_name_dup * like_abs_dup * like_em_dup
    den = num + prior_not * like_name_not * like_abs_not * like_em_not
    prob = num / den if den > 0 else 0.0

    s1, s2 = p1.get("source"), p2.get("source")
    if s1 and s2 and s1 == s2 and s1 in ("fpbase", "pubmed"):
        prob *= 0.01
    return prob


def _abs_max(probe: dict[str, Any]) -> float:
    """Return the absorption maximum as a float, or a large sentinel if absent."""
    try:
        return float(probe.get("abs_max"))
    except (TypeError, ValueError):
        return 999999.0


def group_duplicates(probes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group probes into candidate duplicate sets.

    Parameters
    ----------
    probes : list of dict
        Probe rows, each with at least ``probe_id``, ``chromophore_name``,
        ``category``, ``source`` and (optionally) ``abs_max`` / ``em_max``.

    Returns
    -------
    list of dict
        ``[{"norm_name": <shortest member name>, "probes": [...]}, ...]`` sorted
        by name; only groups with more than one member are returned.

    """
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for probe in probes:
        by_category[probe.get("category", "other")].append(probe)

    edges: list[tuple[Any, Any]] = []
    for cat_probes in by_category.values():
        cat_probes.sort(key=_abs_max)
        count = len(cat_probes)
        for i in range(count):
            p1 = cat_probes[i]
            a1 = _abs_max(p1)
            for j in range(i + 1, count):
                p2 = cat_probes[j]
                a2 = _abs_max(p2)
                if normalize_name(p1["chromophore_name"]) == normalize_name(p2["chromophore_name"]):
                    edges.append((p1["probe_id"], p2["probe_id"]))
                    continue
                if a1 != 999999.0 and a2 != 999999.0 and a2 - a1 > _ABS_MAX_WINDOW_NM:
                    break
                if _duplicate_probability(p1, p2) > _DUPLICATE_PROBABILITY_THRESHOLD:
                    edges.append((p1["probe_id"], p2["probe_id"]))

    parent: dict[Any, Any] = {probe["probe_id"]: probe["probe_id"] for probe in probes}

    def find(item: Any) -> Any:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a: Any, b: Any) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    for a, b in edges:
        union(a, b)

    probe_map = {probe["probe_id"]: probe for probe in probes}
    by_root: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for pid in parent:
        by_root[find(pid)].append(probe_map[pid])

    groups: list[dict[str, Any]] = []
    for members in by_root.values():
        if len(members) > 1:
            members.sort(key=lambda p: len(p["chromophore_name"] or ""))
            groups.append({"norm_name": members[0]["chromophore_name"], "probes": members})

    groups.sort(key=lambda g: (g["norm_name"] or "").lower())
    return groups
