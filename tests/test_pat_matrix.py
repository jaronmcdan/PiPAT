from __future__ import annotations

import time
from pathlib import Path

from roi.core import pat_matrix


def test_decode_pat_j_payload_defaults_to_zeroes():
    assert pat_matrix.decode_pat_j_payload(None) == [0] * 12
    assert pat_matrix.decode_pat_j_payload(b"") == [0] * 12


def test_decode_pat_j_payload_bit_packing():
    # Build u24 where fields are 0,1,2,3 repeated.
    vals = [0, 1, 2, 3] * 3
    u24 = 0
    for i, v in enumerate(vals):
        u24 |= (v & 0x3) << (2 * i)
    b0 = u24 & 0xFF
    b1 = (u24 >> 8) & 0xFF
    b2 = (u24 >> 16) & 0xFF
    got = pat_matrix.decode_pat_j_payload(bytes([b0, b1, b2]))
    assert got == vals


def test_pat_j_ids_and_id_to_index():
    ids = pat_matrix.pat_j_ids()
    assert len(ids) == pat_matrix.PAT_J_COUNT
    assert pat_matrix.PAT_J_BASE_ID in ids
    assert (pat_matrix.PAT_J_BASE_ID + (pat_matrix.PAT_J_STRIDE * (pat_matrix.PAT_J_COUNT - 1))) in ids

    s = pat_matrix.PatSwitchMatrixState()
    assert s._id_to_index(pat_matrix.PAT_J_BASE_ID) == 0
    assert s._id_to_index(pat_matrix.PAT_J_BASE_ID + pat_matrix.PAT_J_STRIDE) == 1
    assert s._id_to_index(pat_matrix.PAT_J_BASE_ID + 123) is None
    assert s._id_to_index(pat_matrix.PAT_J_BASE_ID - pat_matrix.PAT_J_STRIDE) is None
    # tolerate can_id-with-flags style (EFF flag set)
    assert s._id_to_index(0x80000000 | pat_matrix.PAT_J_BASE_ID) == 0


def test_pat_matrix_update_and_snapshot(monkeypatch):
    t = 100.0

    def fake_monotonic():
        return t

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    s = pat_matrix.PatSwitchMatrixState()
    payload = bytes([0xFF, 0x00, 0x00])
    assert s.maybe_update(pat_matrix.PAT_J_BASE_ID, payload, ts=1.25) is True
    snap = s.snapshot()
    assert "J0" in snap
    # 0xFF in the lowest byte sets the first four 2-bit fields to 3.
    assert snap["J0"]["vals"] == [3, 3, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0]
    assert snap["J0"]["age"] == t - 1.25

    # Non-PAT ids are ignored
    assert s.maybe_update(0x123, b"\x00") is False


def test_j0_pin_names_parses_dbc_and_caches():
    # Clear cached global if present
    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    names = pat_matrix.j0_pin_names()
    # The packaged PAT.dbc defines these (and fallback does too)
    assert names.get(1) == "3A_LOAD"
    assert names.get(12) == "PROBE"

    # Second call should return a copy of the cache (mutating shouldn't persist)
    names2 = pat_matrix.j0_pin_names()
    names2[1] = "MUTATED"
    names3 = pat_matrix.j0_pin_names()
    assert names3.get(1) == "3A_LOAD"


def test_parse_j0_pin_names_from_dbc_text_handles_bad_lines():
    txt = """
BO_ 123 PAT_J0: 8 Vector__XXX
 SG_ J0_XX_BAD : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
 SG_ NOT_A_PIN : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
 SG_ J0_01_GOOD : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
BO_ 124 PAT_J1: 8 Vector__XXX
"""

    out = pat_matrix._parse_j0_pin_names_from_dbc_text(txt)
    assert out.get(1) == "GOOD"


def test_parse_j0_pin_names_wrapper_missing_and_existing(tmp_path):
    # Missing file => empty mapping
    assert pat_matrix._parse_j0_pin_names_from_dbc(tmp_path / "nope.dbc") == {}

    dbc = tmp_path / "x.dbc"
    dbc.write_text(
        """
BO_ 123 PAT_J0: 8 Vector__XXX
 SG_ J0_01_FIRST : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
 SG_ J0_02_SECOND : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
""",

        encoding="utf-8",

    )
    out = pat_matrix._parse_j0_pin_names_from_dbc(dbc)
    assert out.get(1) == "FIRST"
    assert out.get(2) == "SECOND"


def test_j0_pin_names_falls_back_when_resource_missing(monkeypatch):
    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    monkeypatch.setattr(pat_matrix, "_read_packaged_pat_dbc_text", lambda: None)

    names = pat_matrix.j0_pin_names()
    assert names.get(12) == "PROBE"


def test_j0_pin_names_parse_exception_falls_back(monkeypatch):
    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    monkeypatch.setattr(pat_matrix, "_read_packaged_pat_dbc_text", lambda: "BO_ 1 PAT_J0: 8 X\n SG_ J0_01_X : 0|2@1+ (1,0) [0|3] \"\" X\n")
    monkeypatch.setattr(pat_matrix, "_parse_j0_pin_names_from_dbc_text", lambda _txt: (_ for _ in ()).throw(RuntimeError("boom")))

    names = pat_matrix.j0_pin_names()
    assert names.get(12) == "PROBE"


def test_id_to_index_int_conversion_error_is_handled():
    class Bad:
        def __int__(self):
            raise ValueError("no")

    s = pat_matrix.PatSwitchMatrixState()
    assert s._id_to_index(Bad()) is None


def test_id_to_index_out_of_range_is_handled():
    s = pat_matrix.PatSwitchMatrixState()
    # One past the last valid PAT_Jx id.
    assert s._id_to_index(pat_matrix.PAT_J_BASE_ID + (pat_matrix.PAT_J_STRIDE * pat_matrix.PAT_J_COUNT)) is None


def test_parse_j0_pin_names_from_dbc_text_int_exception_is_swallowed(monkeypatch):
    """Cover the defensive int() conversion exception path."""

    # A line that *does* match the regex, so we reach the int() conversion.
    txt = """
BO_ 123 PAT_J0: 8 Vector__XXX
 SG_ J0_01_GOOD : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
"""

    def boom_int(x):
        raise ValueError("boom")

    # Patch only inside this module; the function under test uses the global name `int`.
    monkeypatch.setattr(pat_matrix, "int", boom_int, raising=False)

    out = pat_matrix._parse_j0_pin_names_from_dbc_text(txt)
    assert out == {}



def test_parse_j0_pin_names_wrapper_returns_empty_on_parse_exception(monkeypatch, tmp_path):
    """Cover the wrapper's try/except around the text parser."""

    dbc = tmp_path / "x.dbc"
    dbc.write_text(
        """
BO_ 123 PAT_J0: 8 Vector__XXX
 SG_ J0_01_FIRST : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        pat_matrix,
        "_parse_j0_pin_names_from_dbc_text",
        lambda _txt: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )

    assert pat_matrix._parse_j0_pin_names_from_dbc(dbc) == {}


def test_read_packaged_pat_dbc_text_exception_returns_none(monkeypatch):
    """Cover the packaged resource read exception path."""

    monkeypatch.setattr(pat_matrix, "resource_files", lambda _pkg: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pat_matrix._read_packaged_pat_dbc_text() is None


def test_j0_pin_names_cache_assignment_exception_is_swallowed(monkeypatch):
    """Cover the defensive cache write try/except."""

    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    class FlakyCopy(dict):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._calls = 0

        def copy(self):
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("copy")
            return super().copy()

    monkeypatch.setattr(pat_matrix, "_read_packaged_pat_dbc_text", lambda: "whatever", raising=False)
    monkeypatch.setattr(pat_matrix, "_parse_j0_pin_names_from_dbc_text", lambda _txt: FlakyCopy({1: "X"}), raising=False)

    names = pat_matrix.j0_pin_names()
    assert names.get(1) == "X"
