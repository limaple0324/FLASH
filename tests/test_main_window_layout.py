from pathlib import Path


def test_tuple_padding_is_applied_by_pack_not_label_constructor():
    source = Path("main.py").read_text(encoding="utf-8")

    assert 'anchor="w").pack(fill=X, pady=(14, 4))' in source
    assert 'anchor="w", pady=(14, 4)).pack' not in source
