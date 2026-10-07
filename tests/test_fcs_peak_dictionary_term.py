"""Burst-FCS diffusion times resolve through the shared dictionary vocabulary."""

import hashlib
import json
import sqlite3

from mmfdb.schema.pdbx_metadata import MmcifDictionary
from mmfdb.schema.schema_from_dictionary import generate_create_table_for_category

PEAK = "_flr_analysis_feature.diffusion_time_peak"
MEAN = "_flr_analysis_feature.diffusion_time_mean"


def test_peak_term_merges_with_existing_mean_and_generates_real_columns(tmp_path, monkeypatch):
    """FCS quantities extend the feature category without promoting export tables."""
    monkeypatch.setattr(MmcifDictionary, "_cached_dict", None)
    monkeypatch.setattr(MmcifDictionary, "CACHE_PATH", tmp_path / "dictionary.json")
    dictionary = MmcifDictionary.load_bundled()
    peak = dictionary.get_item(PEAK)
    mean = dictionary.get_item(MEAN)
    assert peak is not None
    assert mean is not None
    assert peak.category == mean.category == "flr_analysis_feature"
    assert peak.type_code == mean.type_code == "float"
    assert "milliseconds" in peak.description
    assert "single-component" in peak.description
    assert "weighted mean" in mean.description
    category = dictionary.get_category("flr_analysis_feature")
    assert category.items["diffusion_time_peak"].to_dict() == peak.to_dict()
    assert category.items["diffusion_time_mean"].to_dict() == mean.to_dict()
    assert dictionary.get_category("mmfdb_burst_column") is None
    ddl = generate_create_table_for_category(dictionary, "flr_analysis_feature")
    with sqlite3.connect(":memory:") as connection:
        connection.execute(ddl)
        columns = {row[1]: row[2] for row in connection.execute("PRAGMA table_info(flr_analysis_feature)")}
    assert columns["diffusion_time_peak"] == columns["diffusion_time_mean"] == "REAL"


def test_shipped_cache_matches_the_extension_term_and_content():
    """Packaged cache readers see the same typed term and dictionary revision."""
    cache = json.loads(MmcifDictionary.CACHE_PATH.read_text())
    peak = cache["items"].get(PEAK)
    assert peak is not None
    assert peak["type_code"] == "float"
    assert cache["categories"]["flr_analysis_feature"]["items"]["diffusion_time_peak"] == peak
    path = MmcifDictionary.DATA_DIR / "mmfdb_flr_ext.dic"
    assert cache["hashes"][path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
