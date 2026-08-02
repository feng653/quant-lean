import pandas as pd

from backend.data.cache import has_price_field
from backend.services.simulation import _field_prices


def test_market_panel_exposes_open_and_close_separately():
    date = pd.Timestamp("2026-07-24")
    panel = pd.DataFrame(
        {
            ("000001", "open"): [10.0],
            ("000001", "close"): [10.8],
            ("000002", "open"): [20.0],
            ("000002", "close"): [19.5],
        },
        index=[date],
    )
    panel.columns = pd.MultiIndex.from_tuples(panel.columns)

    assert has_price_field(panel, "open")
    assert _field_prices(panel, date, "open") == {
        "000001": 10.0,
        "000002": 20.0,
    }
    assert _field_prices(panel, date, "close") == {
        "000001": 10.8,
        "000002": 19.5,
    }


def test_legacy_close_pivot_has_no_open_execution_price():
    date = pd.Timestamp("2026-07-24")
    legacy = pd.DataFrame({"000001": [88.0]}, index=[date])

    assert not has_price_field(legacy, "open")
    assert _field_prices(legacy, date, "open") == {}
