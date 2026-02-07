from __future__ import annotations

import time


def test_decode_pat_j_payload_defaults_to_zeroes():
    from pat_matrix import decode_pat_j_payload

    assert decode_pat_j_payload(None) == [0] * 12
    assert decode_pat_j_payload(b"") == [0] * 12


def test_decode_pat_j_payload_bit_packing():
    from pat_matrix import decode_pat_j_payload

    # Build u24 where fields are 0,1,2,3 repeated.
    vals = [0, 1, 2, 3] * 3
    u24 = 0
    for i, v in enumerate(vals):
        u24 |= (v & 0x3) << (2 * i)
    b0 = u24 & 0xFF
    b1 = (u24 >> 8) & 0xFF
    b2 = (u24 >> 16) & 0xFF
    got = decode_pat_j_payload(bytes([b0, b1, b2]))
    assert got == vals


def test_pat_j_ids_and_id_to_index():
    from pat_matrix import PatSwitchMatrixState, PAT_J_BASE_ID, PAT_J_STRIDE, PAT_J_COUNT, pat_j_ids

    ids = pat_j_ids()
    assert len(ids) == PAT_J_COUNT
    assert PAT_J_BASE_ID in ids
    assert (PAT_J_BASE_ID + (PAT_J_STRIDE * (PAT_J_COUNT - 1))) in ids

    s = PatSwitchMatrixState()
    assert s._id_to_index(PAT_J_BASE_ID) == 0
    assert s._id_to_index(PAT_J_BASE_ID + PAT_J_STRIDE) == 1
    assert s._id_to_index(PAT_J_BASE_ID + 123) is None
    assert s._id_to_index(PAT_J_BASE_ID - PAT_J_STRIDE) is None
    # tolerate can_id-with-flags style (EFF flag set)
    assert s._id_to_index(0x80000000 | PAT_J_BASE_ID) == 0


def test_pat_matrix_update_and_snapshot(monkeypatch):
    from pat_matrix import PatSwitchMatrixState, PAT_J_BASE_ID

    t = 100.0

    def fake_monotonic():
        return t

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    s = PatSwitchMatrixState()
    payload = bytes([0xFF, 0x00, 0x00])
    assert s.maybe_update(PAT_J_BASE_ID, payload, ts=1.25) is True
    snap = s.snapshot()
    assert "J0" in snap
    # 0xFF in the lowest byte sets the first four 2-bit fields to 3.
    assert snap["J0"]["vals"] == [3, 3, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0]
    assert snap["J0"]["age"] == t - 1.25

    # Non-PAT ids are ignored
    assert s.maybe_update(0x123, b"\x00") is False


def test_j0_pin_names_parses_dbc_and_caches(monkeypatch):
    import pat_matrix

    # Clear cached global if present
    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    names = pat_matrix.j0_pin_names()
    # The repo's PAT.dbc defines these
    assert names.get(1) == "3A_LOAD"
    assert names.get(12) == "PROBE"

    # Second call should return a copy of the cache (mutating shouldn't persist)
    names2 = pat_matrix.j0_pin_names()
    names2[1] = "MUTATED"
    names3 = pat_matrix.j0_pin_names()
    assert names3.get(1) == "3A_LOAD"


def test_parse_j0_pin_names_handles_missing_and_bad_lines(tmp_path):
    from pat_matrix import _parse_j0_pin_names_from_dbc

    # Missing file => empty mapping
    assert _parse_j0_pin_names_from_dbc(tmp_path / "nope.dbc") == {}

    # Bad lines should be skipped without error.
    dbc = tmp_path / "x.dbc"
    dbc.write_text(
        """
BO_ 123 PAT_J0: 8 Vector__XXX
 SG_ J0_XX_BAD : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
 SG_ NOT_A_PIN : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
 SG_ J0_01_GOOD : 0|2@1+ (1,0) [0|3] \"\" Vector__XXX
BO_ 124 PAT_J1: 8 Vector__XXX
""",
        encoding="utf-8",
    )
    out = _parse_j0_pin_names_from_dbc(dbc)
    assert out.get(1) == "GOOD"


def test_j0_pin_names_falls_back_when_dbc_missing(monkeypatch):
    import pat_matrix

    # Clear cache then force dbc.exists() to return False.
    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")
    monkeypatch.setattr(pat_matrix.Path, "exists", lambda self: False)

    names = pat_matrix.j0_pin_names()
    assert names.get(12) == "PROBE"


def test_id_to_index_int_conversion_error_is_handled():
    from pat_matrix import PatSwitchMatrixState

    class Bad:
        def __int__(self):
            raise ValueError("no")

    s = PatSwitchMatrixState()
    assert s._id_to_index(Bad()) is None


def test_parse_j0_pin_names_int_conversion_error_branch(monkeypatch, tmp_path):
    """Force int(...) to fail to cover the defensive exception block."""
    import pat_matrix

    dbc = tmp_path / "x.dbc"
    dbc.write_text(
        """
BO_ 123 PAT_J0: 8 Vector__XXX
 SG_ J0_01_FIRST : 0|2@1+ (1,0) [0|3] "" Vector__XXX
 SG_ J0_02_SECOND : 0|2@1+ (1,0) [0|3] "" Vector__XXX
""",
        encoding="utf-8",
    )

    real_int = int

    def bad_int(x):
        if str(x) == "01":
            raise ValueError("boom")
        return real_int(x)

    monkeypatch.setattr(pat_matrix, "int", bad_int, raising=False)

    out = pat_matrix._parse_j0_pin_names_from_dbc(dbc)
    assert 1 not in out
    assert out.get(2) == "SECOND"


def test_j0_pin_names_try_block_exception_falls_back(monkeypatch):
    """Exercise the exception handler around DBC parsing in j0_pin_names()."""
    import pat_matrix

    # Clear cache then force Path.resolve() to throw.
    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    def boom(self):
        raise RuntimeError("no")

    monkeypatch.setattr(pat_matrix.Path, "resolve", boom)

    names = pat_matrix.j0_pin_names()
    assert names.get(12) == "PROBE"  # fallback map


def test_j0_pin_names_cache_assignment_failure_is_swallowed(monkeypatch):
    """Cover the try/except around writing the _J0_PIN_NAMES cache."""
    import pat_matrix

    if hasattr(pat_matrix, "_J0_PIN_NAMES"):
        delattr(pat_matrix, "_J0_PIN_NAMES")

    real_dict = dict
    calls = {"n": 0}

    def flaky_dict(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_dict(*args, **kwargs)

    monkeypatch.setattr(pat_matrix, "dict", flaky_dict, raising=False)

    names = pat_matrix.j0_pin_names()
    assert names.get(1) == "3A_LOAD"


def test_id_to_index_out_of_range_is_handled():
    from pat_matrix import PatSwitchMatrixState, PAT_J_BASE_ID, PAT_J_STRIDE, PAT_J_COUNT

    s = PatSwitchMatrixState()
    # One past the last valid PAT_Jx id.
    assert s._id_to_index(PAT_J_BASE_ID + (PAT_J_STRIDE * PAT_J_COUNT)) is None
