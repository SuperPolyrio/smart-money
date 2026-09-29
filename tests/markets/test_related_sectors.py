import pytest

from smart_money.markets.taxonomy import related_profile_sectors


@pytest.mark.parametrize(
    "sector,expected",
    [
        ("ECONOMICS.FED", ("ECONOMICS.FED", "ECONOMICS", "FINANCE.RATES", "ECONOMICS.MACRO", "FINANCE")),
        ("CUSTOM.TOPIC", ("CUSTOM.TOPIC", "CUSTOM")),
        ("CUSTOM", ("CUSTOM",)),
    ],
)
def test_related_sectors_preserve_priority_and_scope(sector, expected):
    assert related_profile_sectors(sector) == expected
