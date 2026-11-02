import pytest
from load_target import target


@pytest.mark.parametrize("value,expected", [("3", 3), ("0", 0), ("999999999", 999999999)])
def test_valid(value, expected):
    assert target().parse_count(value) == expected


@pytest.mark.parametrize("value", ["", "-1", "a", "1.5", "1000000000", " 3", "3 ", "１２"])
def test_invalid(value):
    with pytest.raises(ValueError):
        target().parse_count(value)
