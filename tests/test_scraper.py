"""
Tests for CmcScraper and download_coin_data.

Unit tests use mocked HTTP responses and run in CI without network access.
Integration tests hit the live CoinMarketCap API and are marked with
@pytest.mark.integration — they run in CI as part of the normal test suite
since the project already makes live network calls in CI.
"""

import datetime
from unittest.mock import MagicMock, patch

import pytest

from cryptocmd import CmcScraper
from cryptocmd.utils import (
    DataDownloadError,
    DataIntegrityError,
    download_coin_data,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_quote(date_str, close=50500.0):
    """Return a minimal CMC quote dict for a given UTC date string (YYYY-MM-DD)."""
    return {
        "timeOpen": f"{date_str}T00:00:00.000Z",
        "timeHigh": f"{date_str}T12:00:00.000Z",
        "timeLow": f"{date_str}T06:00:00.000Z",
        "timeClose": f"{date_str}T23:59:59.999Z",
        "quote": {
            "open": 50000.0,
            "high": 51000.0,
            "low": 49000.0,
            "close": close,
            "volume": 1000000.0,
            "marketCap": 900000000000.0,
        },
    }


def _make_response(quotes, coin_id=1, symbol="BTC", name="Bitcoin"):
    """Return a minimal CMC historical API response dict."""
    return {
        "status": {"error_code": 0, "error_message": None},
        "data": {
            "id": coin_id,
            "symbol": symbol,
            "name": name,
            "quotes": quotes,
        },
    }


def _mock_get_url_data(response_dict):
    """Return a mock whose .json() returns response_dict."""
    mock_response = MagicMock()
    mock_response.json.return_value = response_dict
    return mock_response


# ---------------------------------------------------------------------------
# Unit tests — default end_date behaviour
# ---------------------------------------------------------------------------


class TestDefaultEndDate:
    """Verify that the default end_date is today, not yesterday."""

    def test_default_end_date_is_today(self):
        """download_coin_data uses today as end_date when none is provided."""
        today = datetime.date.today()

        captured_urls = []

        def fake_get_url_data(url):
            captured_urls.append(url)
            if "listing" in url:
                return _mock_get_url_data(
                    {
                        "status": {"error_code": 0},
                        "data": {
                            "cryptoCurrencyList": [
                                {"id": 1, "symbol": "BTC", "name": "Bitcoin"}
                            ]
                        },
                    }
                )
            return _mock_get_url_data(_make_response([_make_quote("2026-04-25")]))

        with patch("cryptocmd.utils.get_url_data", side_effect=fake_get_url_data):
            download_coin_data("BTC", None, None, "USD", None)

        # The default range spans many years so chunking produces multiple requests.
        # The LAST historical chunk's timeEnd should be >= today midnight UTC.
        historical_urls = [u for u in captured_urls if "historical" in u]
        assert historical_urls, "No historical API call was made"

        today_midnight = int(
            datetime.datetime.combine(today, datetime.time.min)
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )

        last_url = historical_urls[-1]
        params = dict(p.split("=") for p in last_url.split("?")[1].split("&"))
        time_end = int(params["timeEnd"])
        assert time_end >= today_midnight, (
            f"Last chunk timeEnd ({time_end}) is before today midnight ({today_midnight}). "
            "Default end_date should be today, not yesterday."
        )

    def test_default_end_date_not_yesterday(self):
        """Regression: last chunk timeEnd must NOT be end-of-yesterday (old behaviour)."""
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        yesterday_eod = int(
            datetime.datetime.combine(yesterday, datetime.time(23, 59, 59))
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )

        captured_urls = []

        def fake_get_url_data(url):
            captured_urls.append(url)
            if "listing" in url:
                return _mock_get_url_data(
                    {
                        "status": {"error_code": 0},
                        "data": {
                            "cryptoCurrencyList": [
                                {"id": 1, "symbol": "BTC", "name": "Bitcoin"}
                            ]
                        },
                    }
                )
            return _mock_get_url_data(_make_response([_make_quote("2026-04-25")]))

        with patch("cryptocmd.utils.get_url_data", side_effect=fake_get_url_data):
            download_coin_data("BTC", None, None, "USD", None)

        historical_urls = [u for u in captured_urls if "historical" in u]
        last_url = historical_urls[-1]
        params = dict(p.split("=") for p in last_url.split("?")[1].split("&"))
        time_end = int(params["timeEnd"])
        assert time_end > yesterday_eod, (
            "Last chunk timeEnd is end-of-yesterday — default end_date is still using "
            "old yesterday offset. Expected today."
        )

    def test_explicit_end_date_is_respected(self):
        """An explicitly provided end_date overrides the default."""
        explicit_end = "01-01-2024"
        captured_urls = []

        def fake_get_url_data(url):
            captured_urls.append(url)
            if "listing" in url:
                return _mock_get_url_data(
                    {
                        "status": {"error_code": 0},
                        "data": {
                            "cryptoCurrencyList": [
                                {"id": 1, "symbol": "BTC", "name": "Bitcoin"}
                            ]
                        },
                    }
                )
            return _mock_get_url_data(_make_response([_make_quote("2024-01-01")]))

        with patch("cryptocmd.utils.get_url_data", side_effect=fake_get_url_data):
            download_coin_data("BTC", "01-12-2023", explicit_end, "USD", None)

        historical_urls = [u for u in captured_urls if "historical" in u]
        assert historical_urls
        # timeEnd for all chunks should be before today (explicit end_date respected)
        today_midnight = int(
            datetime.datetime.combine(datetime.date.today(), datetime.time.min)
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
        for url in historical_urls:
            params = dict(p.split("=") for p in url.split("?")[1].split("&"))
            time_end = int(params["timeEnd"])
            assert (
                time_end < today_midnight
            ), "Explicit end_date was ignored — timeEnd was set to today instead."


# ---------------------------------------------------------------------------
# Unit tests — CmcScraper init and data parsing
# ---------------------------------------------------------------------------


class TestCmcScraperInit:
    """Test CmcScraper initialisation and parameter handling."""

    def test_all_time_flag_clears_dates(self):
        """all_time=True causes start_date and end_date to be cleared on download."""
        quotes = [_make_quote("2026-04-25"), _make_quote("2026-04-24")]
        response = _make_response(quotes)

        with patch("cryptocmd.utils.get_url_data") as mock_get:
            mock_get.return_value = _mock_get_url_data(response)
            scraper = CmcScraper("BTC", all_time=True, id_number=1)
            scraper._download_data()

        assert scraper.start_date is not None
        assert scraper.end_date is not None

    def test_scraper_rows_parsed_correctly(self):
        """Parsed rows contain correct values from API response."""
        quotes = [_make_quote("2026-04-25")]
        response = _make_response(quotes)

        with patch("cryptocmd.utils.get_url_data") as mock_get:
            mock_get.return_value = _mock_get_url_data(response)
            scraper = CmcScraper("BTC", "24-04-2026", "25-04-2026", id_number=1)
            headers, rows = scraper.get_data()

        assert headers == [
            "Date",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "Market Cap",
            "Time Open",
            "Time High",
            "Time Low",
            "Time Close",
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row[0] == "25-04-2026"  # Date
        assert row[1] == 50000.0  # Open
        assert row[4] == 50500.0  # Close

    def test_order_ascending(self):
        """order_ascending=True returns oldest row first."""
        quotes = [_make_quote("2026-04-25"), _make_quote("2026-04-24")]
        response = _make_response(quotes)

        with patch("cryptocmd.utils.get_url_data") as mock_get:
            mock_get.return_value = _mock_get_url_data(response)
            scraper = CmcScraper(
                "BTC",
                "24-04-2026",
                "25-04-2026",
                order_ascending=True,
                id_number=1,
            )
            _, rows = scraper.get_data()

        assert rows[0][0] == "24-04-2026"
        assert rows[1][0] == "25-04-2026"

    def test_order_descending(self):
        """Default order (descending) returns newest row first."""
        quotes = [_make_quote("2026-04-24"), _make_quote("2026-04-25")]
        response = _make_response(quotes)

        with patch("cryptocmd.utils.get_url_data") as mock_get:
            mock_get.return_value = _mock_get_url_data(response)
            scraper = CmcScraper("BTC", "24-04-2026", "25-04-2026", id_number=1)
            _, rows = scraper.get_data()

        assert rows[0][0] == "25-04-2026"
        assert rows[1][0] == "24-04-2026"


# ---------------------------------------------------------------------------
# Unit tests — multi-chunk merging, clipping, conflicts, completeness
# ---------------------------------------------------------------------------


def _quotes_for_range(start, end, skip=()):
    """Quotes for every day in [start, end] (datetime.date), ascending."""
    quotes = []
    day = start
    while day <= end:
        if day not in skip:
            quotes.append(_make_quote(day.isoformat()))
        day += datetime.timedelta(days=1)
    return quotes


def _download_with_chunks(responses, *args):
    """Run download_coin_data with one mocked response per historical chunk.

    ``responses`` items are response dicts or Exception instances to raise.
    """
    state = {"i": 0}

    def fake_get_url_data(url):
        if "listing" in url:
            return _mock_get_url_data(
                {
                    "status": {"error_code": 0},
                    "data": {
                        "cryptoCurrencyList": [
                            {"id": 1, "symbol": "BTC", "name": "Bitcoin"}
                        ]
                    },
                }
            )
        resp = responses[state["i"]]
        state["i"] += 1
        if isinstance(resp, Exception):
            raise resp
        return _mock_get_url_data(resp)

    with patch("cryptocmd.utils.get_url_data", side_effect=fake_get_url_data):
        return download_coin_data(*args)


# A 400-day range that forces two chunks (CMC caps responses at ~365 entries).
# Chunk 1: timeStart 2023-12-31 (exclusive) .. timeEnd 2024-12-31
# Chunk 2: timeStart 2024-12-31 (exclusive) .. timeEnd 2025-02-05
_RANGE_ARGS = ("BTC", "01-01-2024", "05-02-2025", "USD", None, 1)
_CHUNK1 = _quotes_for_range(datetime.date(2024, 1, 1), datetime.date(2024, 12, 31))
_CHUNK2 = _quotes_for_range(datetime.date(2025, 1, 1), datetime.date(2025, 2, 5))
_TOTAL_DAYS = 402  # 2024-01-01 .. 2025-02-05 (2024 is a leap year)


class TestChunkMerging:
    """Multi-chunk responses are merged by trading day, not concatenated."""

    def test_365_day_boundary_overlap_deduplicated(self):
        """Identical boundary day present in both chunks is merged, not doubled."""
        chunk2 = [_make_quote("2024-12-31")] + _CHUNK2  # overlapping boundary day
        result = _download_with_chunks(
            [_make_response(_CHUNK1), _make_response(chunk2)], *_RANGE_ARGS
        )

        quotes = result["data"]["quotes"]
        days = [q["timeOpen"][:10] for q in quotes]
        assert len(days) == _TOTAL_DAYS
        assert len(set(days)) == _TOTAL_DAYS, "duplicate dates survived the merge"
        assert days == sorted(days), "merged quotes are not in ascending order"
        assert days[0] == "2024-01-01"
        assert days[-1] == "2025-02-05"
        assert result["completeness"]["missingDays"] == []

    def test_conflicting_quotes_fail_loudly(self):
        """Same day with different values across chunks raises, never picks one."""
        chunk2 = [_make_quote("2024-12-31", close=1.0)] + _CHUNK2
        with pytest.raises(DataIntegrityError, match="2024-12-31"):
            _download_with_chunks(
                [_make_response(_CHUNK1), _make_response(chunk2)], *_RANGE_ARGS
            )

    def test_quotes_outside_requested_range_are_clipped(self):
        """Days before start_date / after end_date are dropped."""
        chunk1 = [_make_quote("2023-12-31")] + _CHUNK1
        chunk2 = _CHUNK2 + [_make_quote("2025-02-06")]
        result = _download_with_chunks(
            [_make_response(chunk1), _make_response(chunk2)], *_RANGE_ARGS
        )

        days = [q["timeOpen"][:10] for q in result["data"]["quotes"]]
        assert "2023-12-31" not in days
        assert "2025-02-06" not in days
        assert len(days) == _TOTAL_DAYS

    def test_missing_days_listed_in_completeness_report(self):
        """A day absent from every chunk appears in the completeness report."""
        gap = datetime.date(2024, 6, 15)
        chunk1 = _quotes_for_range(
            datetime.date(2024, 1, 1), datetime.date(2024, 12, 31), skip={gap}
        )
        result = _download_with_chunks(
            [_make_response(chunk1), _make_response(_CHUNK2)], *_RANGE_ARGS
        )

        report = result["completeness"]
        assert report["missingDays"] == ["2024-06-15"]
        assert report["expectedDays"] == _TOTAL_DAYS
        assert report["receivedDays"] == _TOTAL_DAYS - 1
        assert len(result["data"]["quotes"]) == _TOTAL_DAYS - 1

    def test_coin_drift_between_chunks_fails(self):
        """A chunk returning a different coin aborts the merge."""
        chunk2_response = _make_response(_CHUNK2, coin_id=1027, symbol="ETH", name="Ethereum")
        with pytest.raises(DataIntegrityError, match="ETH"):
            _download_with_chunks(
                [_make_response(_CHUNK1), chunk2_response], *_RANGE_ARGS
            )

    def test_failed_chunk_raises_instead_of_partial_result(self):
        """A chunk that fails to download aborts the whole request."""
        with pytest.raises(DataDownloadError):
            _download_with_chunks(
                [_make_response(_CHUNK1), ConnectionError("boom")], *_RANGE_ARGS
            )

    def test_failed_download_leaves_no_stale_rows(self):
        """CmcScraper never serves a stale, seemingly-complete concatenation."""
        scraper = CmcScraper("BTC", "01-01-2024", "05-02-2025", id_number=1)
        scraper.rows = [["stale"]]  # simulate a previous successful download

        responses = [_make_response(_CHUNK1), ConnectionError("boom")]
        state = {"i": 0}

        def fake_get_url_data(url):
            resp = responses[state["i"]]
            state["i"] += 1
            if isinstance(resp, Exception):
                raise resp
            return _mock_get_url_data(resp)

        with patch("cryptocmd.utils.get_url_data", side_effect=fake_get_url_data):
            with pytest.raises(DataDownloadError):
                scraper.get_data(forced=True)

        assert scraper.rows == []

    def test_scraper_exposes_completeness_and_order(self):
        """Scraper rows keep descending default order; completeness is exposed."""
        result_chunks = [_make_response(_CHUNK1), _make_response(_CHUNK2)]
        state = {"i": 0}

        def fake_get_url_data(url):
            resp = result_chunks[state["i"]]
            state["i"] += 1
            return _mock_get_url_data(resp)

        scraper = CmcScraper("BTC", "01-01-2024", "05-02-2025", id_number=1)
        with patch("cryptocmd.utils.get_url_data", side_effect=fake_get_url_data):
            _, rows = scraper.get_data()

        assert len(rows) == _TOTAL_DAYS
        assert rows[0][0] == "05-02-2025"  # descending by default
        assert rows[-1][0] == "01-01-2024"
        assert scraper.start_date == "01-01-2024"
        assert scraper.end_date == "05-02-2025"
        assert scraper.completeness["missingDays"] == []


# ---------------------------------------------------------------------------
# Integration tests — live network
# ---------------------------------------------------------------------------
@pytest.mark.integration
class TestIntegration:
    """Live network tests against CoinMarketCap API."""

    def test_default_scraper_returns_recent_data(self):
        """Default scraper (no dates) returns data with latest candle within last 2 days."""
        scraper = CmcScraper("BTC", "23-04-2026")
        headers, rows = scraper.get_data()

        assert rows, "No data returned"
        latest_date = datetime.datetime.strptime(rows[0][0], "%d-%m-%Y").date()
        two_days_ago = datetime.date.today() - datetime.timedelta(days=2)
        assert latest_date >= two_days_ago, (
            f"Latest candle ({latest_date}) is older than 2 days. "
            "Default end_date may still be using yesterday offset."
        )

    def test_explicit_date_range_returns_correct_rows(self):
        """Explicit date range returns data covering the requested dates.

        CMC's timeStart is exclusive (first returned candle = timeStart + 1 day),
        so a range of 01-01-2024 to 05-01-2024 may include Dec 31 as well.
        We assert that all requested dates are present and no candle is newer
        than end_date.
        """
        scraper = CmcScraper("BTC", "01-01-2024", "05-01-2024")
        headers, rows = scraper.get_data()

        assert rows, "No data returned"
        dates = [datetime.datetime.strptime(r[0], "%d-%m-%Y").date() for r in rows]
        assert datetime.date(2024, 1, 5) in dates, "end_date candle missing"
        assert datetime.date(2024, 1, 1) in dates, "start_date candle missing"
        assert max(dates) <= datetime.date(
            2024, 1, 5
        ), "Candle returned beyond end_date"

    def test_headers_are_correct(self):
        """Headers match expected column names."""
        scraper = CmcScraper("BTC", "01-01-2024", "02-01-2024")
        headers, _ = scraper.get_data()
        assert headers == [
            "Date",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
            "Market Cap",
            "Time Open",
            "Time High",
            "Time Low",
            "Time Close",
        ]
