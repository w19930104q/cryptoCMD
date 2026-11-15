#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function

import json
import os
import sys
import datetime
from requests import get


_CMC_DATA_API = "https://api.coinmarketcap.com/data-api/v3"

with open(os.path.join(os.path.dirname(__file__), "fiat_ids.json")) as _f:
    _FIAT_IDS = json.load(_f)

_SUPPORTED_FIATS = ", ".join(sorted(_FIAT_IDS))

_HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_url_data(url):
    """
    This method downloads the data of the web page.
    :param url: 'url' of the web page to download
    :return: response object of get request of the 'url'
    """

    try:
        response = get(url, headers=_HEADERS)
        return response
    except Exception as e:
        print("Error message (get_url_data) :", e)
        raise e


def get_coin_id(coin_code, coin_name):
    """
    This method fetches the id of currency from the given code
    :param coin_code: coin code of a cryptocurrency e.g. btc
    :param coin_name: coin name in case of many coins with same code e.g. sol -> solana, solcoin
    :return: numeric coin id for the cryptocurrency on coinmarketcap.com
    """

    api_url = (
        "{}/cryptocurrency/listing?start=1&limit=5000"
        "&sortBy=market_cap&sortType=desc&convert=USD"
        "&cryptoType=all&tagType=all&audited=false"
    ).format(_CMC_DATA_API)

    try:
        json_data = get_url_data(api_url).json()
        error_code = json_data["status"]["error_code"]
        if str(error_code) == "0":
            coins = json_data["data"]["cryptoCurrencyList"]
            matches = [
                c
                for c in coins
                if c["symbol"].upper() == coin_code.upper()
                and (coin_name is None or c["name"].lower() == coin_name.lower())
            ]
            if matches:
                return matches[0]["id"]
            raise InvalidCoinCode(
                "'{}' coin code is unavailable on coinmarketcap.com".format(coin_code)
            )
        if str(error_code) == "400":
            raise InvalidCoinCode(
                "'{}' coin code is unavailable on coinmarketcap.com".format(coin_code)
            )
        raise Exception(json_data["status"]["error_message"])
    except Exception as e:
        print("Error fetching coin id data for coin code {}".format(coin_code))
        print("Error message:", e)
        raise e


def download_coin_data(
    coin_code, start_date, end_date, fiat, coin_name, id_number=None
):
    """
    Download price history for the specified cryptocurrency and time range from CoinMarketCap.

    :param coin_code: coin code of a cryptocurrency e.g. btc
    :param start_date: date since when to scrape data (in the format of dd-mm-yyyy)
    :param end_date: date to which scrape the data (in the format of dd-mm-yyyy).
        Defaults to today. CMC only returns completed daily candles, so the latest
        candle returned will always be the last completed UTC day regardless of this value.
    :param fiat: fiat code eg. USD, EUR
    :param coin_name: coin name in case of many coins with same code e.g. sol -> solana, solcoin
    :param id_number: id number for the token on coinmarketcap. Will override coin_code and coin_name when provided.

    :return: json data with historical OHLCV data for the cryptocurrency
    """

    if start_date is None:
        # default start date on coinmarketcap.com
        start_date = "28-4-2013"

    if end_date is None:
        end_date = datetime.date.today().strftime("%d-%m-%Y")

    coin_id = id_number if id_number else get_coin_id(coin_code, coin_name)
    convert_id = _FIAT_IDS.get(fiat.upper())
    if convert_id is None:
        raise ValueError(f"Unknown fiat '{fiat}'. Supported: {_SUPPORTED_FIATS}.")

    start_dt = datetime.datetime.strptime(start_date, "%d-%m-%Y").replace(
        tzinfo=datetime.timezone.utc
    )
    end_dt = datetime.datetime.strptime(end_date, "%d-%m-%Y").replace(
        tzinfo=datetime.timezone.utc
    )
    start_day = start_dt.date()
    end_day = end_dt.date()

    try:
        # CMC caps responses at ~365 entries per request; split into yearly chunks
        # so long/all-time ranges always return daily granularity.
        # timeStart is exclusive (first returned day = timeStart + 1), so each
        # chunk's timeStart equals the previous chunk's timeEnd. Chunks therefore
        # overlap by one day when the API treats timeStart as inclusive; the
        # day-keyed merge below deduplicates that overlap and refuses to
        # silently pick between conflicting quotes.
        chunks = []
        result_json = None
        chunk_meta = None
        chunk_start = start_dt - datetime.timedelta(days=1)

        while chunk_start < end_dt:
            chunk_end = min(chunk_start + datetime.timedelta(days=365), end_dt)
            api_url = (
                "{}/cryptocurrency/historical"
                "?id={}&convertId={}&timeStart={}&timeEnd={}"
            ).format(
                _CMC_DATA_API,
                coin_id,
                convert_id,
                int(chunk_start.timestamp()),
                int(chunk_end.timestamp()),
            )
            try:
                json_data = get_url_data(api_url).json()
            except Exception as e:
                raise DataDownloadError(
                    "Failed to download chunk {}..{}: {}".format(
                        chunk_start.date().isoformat(),
                        chunk_end.date().isoformat(),
                        e,
                    )
                ) from e
            status = json_data.get("status", {})
            error_code = status.get("error_code")
            if error_code and str(error_code) != "0":
                raise Exception(status.get("error_message", "Unknown error"))
            data = json_data["data"]
            meta = (data.get("id"), data.get("symbol"), data.get("name"))
            if result_json is None:
                result_json = json_data
                chunk_meta = meta
            elif meta != chunk_meta:
                raise DataIntegrityError(
                    "Chunk {}..{} returned coin {} but previous chunks returned "
                    "{}; refusing to merge data across instruments.".format(
                        chunk_start.date().isoformat(),
                        chunk_end.date().isoformat(),
                        meta,
                        chunk_meta,
                    )
                )
            chunks.append(data["quotes"])
            chunk_start = chunk_end

        merged = _merge_quotes(chunks, start_day, end_day)
        result_json["data"]["quotes"] = [merged[day] for day in sorted(merged)]
        result_json["completeness"] = _completeness_report(
            merged, start_day, end_day
        )

        if id_number:
            show_coin_info = False
            if coin_code and coin_code.upper() != result_json["data"]["symbol"].upper():
                print(
                    f"INFO: Using 'id_number'! The 'coin_code' ({coin_code}) provided "
                    + "is different from the symbol returned."
                )
                show_coin_info = True
            if coin_name and coin_name.lower() != result_json["data"]["name"].lower():
                print(
                    f"INFO: Using 'id_number'! The 'coin_name' ({coin_name}) provided "
                    + "is different from the name returned."
                )
                show_coin_info = True
            if show_coin_info:
                print(
                    f"""The returned data belongs to coin "{result_json['data']['name']}", """
                    + f"""with symbol "{result_json['data']['symbol']}" """
                )
        return result_json
    except Exception as e:
        print(
            "Error fetching price data for {} for interval '{}' and '{}'".format(
                f"(id {id_number})" if id_number else coin_code,
                start_date,
                end_date,
            )
        )
        print("Error message (download_data) :", e)
        raise e


def _quote_day(quote):
    """Trading day (UTC date) of a CMC quote, used as the canonical merge key."""
    return datetime.datetime.strptime(quote["timeOpen"][:10], "%Y-%m-%d").date()


def _merge_quotes(chunks, start_day, end_day):
    """
    Merge per-chunk quote lists keyed by trading day.

    Quotes outside [start_day, end_day] are clipped. Identical duplicates
    (e.g. from the one-day chunk overlap) are deduplicated; two different
    quotes for the same day raise DataIntegrityError rather than picking
    one arbitrarily.
    """
    merged = {}
    for quotes in chunks:
        for quote in quotes:
            day = _quote_day(quote)
            if day < start_day or day > end_day:
                continue
            existing = merged.get(day)
            if existing is None:
                merged[day] = quote
            elif existing != quote:
                raise DataIntegrityError(
                    "Conflicting quotes for {} across response chunks; "
                    "refusing to pick one arbitrarily.".format(day.isoformat())
                )
    return merged


def _completeness_report(merged, start_day, end_day):
    """Report which days in the requested range have no quote."""
    total_days = (end_day - start_day).days + 1
    missing = [
        (start_day + datetime.timedelta(days=i)).isoformat()
        for i in range(total_days)
        if start_day + datetime.timedelta(days=i) not in merged
    ]
    return {
        "startDate": start_day.isoformat(),
        "endDate": end_day.isoformat(),
        "expectedDays": total_days,
        "receivedDays": len(merged),
        "missingDays": missing,
    }


def _replace(s, bad_chars):
    if sys.version_info > (3, 0):
        # For Python 3
        without_bad_chars = str.maketrans("", "", bad_chars)
        return s.translate(without_bad_chars)
    else:
        # For Python 2
        import string

        identity = string.maketrans("", "")
        return s.translate(identity, bad_chars)


class InvalidParameters(ValueError):
    """Passed parameters are invalid."""


class InvalidCoinCode(NotImplementedError):
    """This coin code is unavailable on 'coinmarketcap.com'"""


class DataIntegrityError(Exception):
    """Merged market data failed integrity checks (conflicting quotes or
    instrument drift between response chunks)."""


class DataDownloadError(Exception):
    """A chunk of the requested range could not be downloaded; no partial
    result is returned."""
