from __future__ import annotations

import io

from mmfdb.cif_writer import CifWriter


def test_cif_writer_quotes_reserved_and_multiline_values() -> None:
    output = io.StringIO()
    writer = CifWriter(output)
    writer.start_block("example")
    with writer.loop("_entry", ["id", "details", "missing"]) as loop:
        loop.write(id="loop_value", details="line one\nline two", missing=None)

    text = output.getvalue()
    assert text.startswith("data_example\n#\nloop_\n")
    assert "'loop_value'" in text
    assert ";line one\nline two\n;" in text
    assert text.endswith(".\n#\n")


def test_cif_writer_omits_empty_loops() -> None:
    output = io.StringIO()
    writer = CifWriter(output)
    writer.start_block("example")
    with writer.loop("_empty", ["id"]):
        pass
    with writer.loop("_entry", ["id"]) as loop:
        loop.write(id=1)

    text = output.getvalue()
    assert "_empty.id" not in text
    assert "_entry.id" in text
