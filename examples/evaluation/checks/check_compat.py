from load_target import target


def test_existing_label_api():
    assert target().stable_label(3) == "item:3"
    assert target().stable_label("日本語") == "item:日本語"
