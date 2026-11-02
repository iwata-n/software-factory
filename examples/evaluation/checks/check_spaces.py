import pytest
from load_target import target


@pytest.mark.parametrize(
    "value,expected",
    [
        (" a  b ", "a b"),
        ("", ""),
        ("   ", ""),
        (" あ  い ", "あ い"),
        ("\ta\tb\n", "\ta\tb\n"),
        ("　a　", "　a　"),
        ("a    b", "a b"),
    ],
)
def test_ascii_space_only(value, expected):
    assert target().normalize_spaces(value) == expected
