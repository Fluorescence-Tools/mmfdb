"""A term in this dictionary is a name, not somebody's internal abbreviation.

The dictionary is the product. Every controlled value in it is read by people
and by programs that were not written here, and a value only they can decode is
not a vocabulary entry -- it is a private note in a public register.

This has already happened twice, both times because nothing checked:

* tttrlib wrote `bva`, `kde_cde` and `mle_green` into `.pto` containers for
  operations this dictionary calls `burst_variance_analysis`, `burst_2cde` and
  `burst_lifetime_fitting`, and kept its own copy of the dictionary that agreed
  with them.
* A first draft of `_mmfdb_label_score.definition` enumerated `labelizer_tp`,
  `labelizer_ce`, `labelizer_cs` -- one value per *file tag* of the program that
  computes them. `tp` is Labelizer's private name for tryptophan proximity and
  means nothing to a reader here.

Both are the same failure and it is mechanically detectable: a token nobody
outside one program can expand. So this refuses it.

The rules are deliberately few, because a naming test that is hard to satisfy
gets worked around:

1. lowercase, digits, underscores, and a dot where a value is genuinely
   namespaced -- no camelCase, no other punctuation;
2. every underscore-separated token is a word, an established acronym, or a
   file-format name -- the short ones are an explicit ledger below, and adding
   to it is a deliberate act rather than a side effect;
3. a value does not repeat a quantity another item in the same category already
   states, which is how `labelizer_tryptophan_proximity` would have gone wrong
   in the other direction.
"""

from __future__ import annotations

import pathlib
import re

import pytest

DATA = pathlib.Path(__file__).resolve().parents[1] / "src" / "mmfdb" / "data"

#: Tokens of three characters or fewer that are allowed, and why.
#:
#: This is a **ledger, not a convenience**. Every entry is an established
#: acronym, a file-format name, a unit, or an ordinary English word -- something
#: a reader outside this project can expand without being told. Adding one is
#: how the vocabulary grows honestly; adding `tp` because a program calls it
#: that is what this exists to prevent.
SHORT_TOKENS = {
    # established acronyms in this field
    "fcs", "hmm", "pda", "mle", "pch", "psf", "irf", "gmm", "dna", "rna",
    # file formats and encodings
    "ptu", "spc", "pto", "bur", "csv", "tsv", "cif", "png", "svg", "zip",
    "npy", "bin", "raw", "map", "mti", "md5", "url", "bh",
    # ordinary words
    "and", "by", "in", "of", "on", "per", "to", "no", "yes", "row", "run",
    "fit", "int", "kit", "tau", "cmd", "all", "set", "add", "app",
}

#: Values that are literally numeric, which no naming rule sensibly applies to.
NUMERIC = re.compile(r"^-?\d+$")


def _enumerations() -> list[tuple[str, str, str]]:
    """``(file, item, value)`` for every enumerated value in mmfdb's own dicts.

    Only ``mmfdb*.dic``: the bundled PDBx/IHM dictionaries are upstream's and
    their names are not ours to police.
    """
    out: list[tuple[str, str, str]] = []
    for path in sorted(DATA.glob("mmfdb*.dic")):
        text = path.read_text(errors="replace")
        for block in re.finditer(
                r"save_(_[a-z_]+\.[a-z_]+)\b(.*?)(?=\nsave_|\Z)", text, re.S):
            item, body = block.group(1), block.group(2)
            if "_item_enumeration.value" not in body:
                continue
            tail = body.split("_item_enumeration.value", 1)[1]
            tail = tail.split("_item_enumeration.detail", 1)[-1]
            for line in tail.splitlines():
                line = line.strip()
                if not line or line[0] in "_#;":
                    continue
                if line.startswith(("save_", "loop_")):
                    break
                out.append((path.name, item, line.split()[0].strip('"')))
    return out


@pytest.fixture(scope="module")
def enumerations():
    found = _enumerations()
    assert found, "found no enumerations at all; the scan is broken"
    return found


def test_every_value_is_lowercase_snake_case(enumerations):
    """Lowercase, digits, underscores -- and a dot where a value is namespaced.

    `_mmfdb_event_log.action_type` names events in a session (`fit.close_all`,
    `app.reinitialize.start`), where the dot separates a scope from an action.
    That is a real convention and a readable one, which is what the rule is
    actually about; forbidding it would be pedantry rather than clarity.
    """
    bad = [(f, i, v) for f, i, v in enumerations
           if not NUMERIC.match(v) and not re.fullmatch(r"[a-z0-9_.]+", v)]
    assert not bad, (
        "enumeration values must be lowercase, digits, underscores and dots:\n"
        + "\n".join(f"  {i} = {v!r}  ({f})" for f, i, v in bad)
    )


def test_no_value_contains_an_unexpandable_abbreviation(enumerations):
    """The rule that would have caught `labelizer_tp`, `bva` and `kde_cde`."""
    offenders = []
    for f, item, value in enumerations:
        if NUMERIC.match(value):
            continue
        for token in re.split(r"[_.]", value):
            if NUMERIC.match(token) or len(token) > 3 or token in SHORT_TOKENS:
                continue
            offenders.append((f, item, value, token))
    assert not offenders, (
        "these values contain a short token that is not in the ledger, so a "
        "reader outside the program that coined it cannot expand it. Spell it "
        "out, or add the token to SHORT_TOKENS with a reason:\n"
        + "\n".join(f"  {i} = {v!r}  (token {t!r}, {f})"
                    for f, i, v, t in offenders)
    )


#: (coarse item, qualifier item) pairs: the qualifier says what the coarse one
#: does not, and must not restate it.
#:
#: Declared rather than inferred. A generic "no value ends with another value"
#: rule flags `storage_mode = local_directory` against
#: `data_format = directory` -- two different concepts that share a word -- and
#: a check that fails on correct names only teaches people to add exceptions.
QUALIFIED_PAIRS = (
    ("_mmfdb_operation.operation_type", "_mmfdb_operation.algorithm"),
    ("_mmfdb_label_score.score_type", "_mmfdb_label_score.definition"),
)


def test_a_qualifier_does_not_restate_what_it_qualifies(enumerations):
    """`labelizer_tryptophan_proximity` beside `score_type = tryptophan_proximity`.

    Where a category has a coarse item and a qualifier, the two together are the
    fact. If the qualifier repeats the coarse value they can disagree, and the
    redundant one is the one that will be wrong. This is the shape the first
    draft of `_mmfdb_label_score.definition` had.
    """
    values: dict[str, set[str]] = {}
    for _f, item, value in enumerations:
        values.setdefault(item, set()).add(value)

    offenders = []
    for coarse, qualifier in QUALIFIED_PAIRS:
        for value in sorted(values.get(qualifier, ())):
            for coarse_value in values.get(coarse, ()):
                if value.endswith("_" + coarse_value) or value == coarse_value:
                    offenders.append((qualifier, value, coarse, coarse_value))
    assert not offenders, (
        "a qualifier restates the term it qualifies:\n"
        + "\n".join(f"  {q} = {v!r} repeats {c} = {cv!r}"
                    for q, v, c, cv in offenders)
    )


def test_the_ledger_has_no_dead_entries(enumerations):
    """A ledger nobody checks becomes a place to hide things.

    Every short token allowed above must actually be in use, so the list stays a
    record of decisions taken rather than a pre-emptive allowance.
    """
    used = {token
            for _f, _i, value in enumerations
            for token in re.split(r"[_.]", value)}
    dead = sorted(SHORT_TOKENS - used)
    assert not dead, (
        f"SHORT_TOKENS allows {dead}, which no enumeration uses. Remove them; "
        "an allowance granted in advance is not a decision."
    )
