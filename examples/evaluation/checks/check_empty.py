from load_target import target


def test_empty():
    assert target().describe_items([]) == "データがありません"


def test_one_item():
    assert target().describe_items(["A"]) == "A"


def test_many_items():
    assert target().describe_items(["A", "B"]) == "A, B"
