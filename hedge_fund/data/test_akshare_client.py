"""A-share data client mapping contracts.

These tests pin the pure mapping logic of AkshareDataClient: ticker
normalization, price-row mapping, financial-metrics mapping, and the
point-in-time filing-date approximation. No network calls — fixtures are
samples of what the free AkShare endpoints actually return.

Point-in-time note: AkShare's analysis-indicator endpoint has no filing
date. A-share filing deadlines are fixed by CSRC rules (Q1: 04-30,
H1: 08-31, Q3: 10-31, annual: 04-30 next year), so the deadline is used as
a conservative public-by date: never earlier than the real filing, hence
no look-ahead in backtests (at the cost of a slightly stale signal).
"""

from __future__ import annotations

import pytest

from hedge_fund.data.akshare_client import (
    AkshareDataClient,
    _apply_valuation,
    _baostock_symbol,
    _filing_deadline,
    _hk_code,
    _map_company_facts,
    _map_financial_metrics,
    _map_hk_tencent_prices,
    _map_price,
    _match_valuation,
    _merge_abstract,
    _prefixed_symbol,
    normalize_ticker,
)
from hedge_fund.data.models import CompanyFacts, FinancialMetrics, Price


# ---------------------------------------------------------------------------
# Network retry behavior
# ---------------------------------------------------------------------------

def test_fetch_retries_then_succeeds() -> None:
    client = AkshareDataClient()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("remote closed connection")
        return "ok"

    assert client._fetch(flaky, retries=2, delay=0) == "ok"
    assert calls["n"] == 2


def test_fetch_gives_up_after_retries() -> None:
    client = AkshareDataClient()

    def always_fail():
        raise ConnectionError("remote closed connection")

    with pytest.raises(ConnectionError):
        client._fetch(always_fail, retries=2, delay=0)


# ---------------------------------------------------------------------------
# Ticker normalization
# ---------------------------------------------------------------------------

def test_normalize_ticker_accepts_plain_6_digit() -> None:
    assert normalize_ticker("600519") == "600519"


def test_normalize_ticker_accepts_exchange_prefix() -> None:
    assert normalize_ticker("sh600519") == "600519"
    assert normalize_ticker("sz000001") == "000001"


def test_normalize_ticker_accepts_suffix_variants() -> None:
    assert normalize_ticker("600519.SH") == "600519"
    assert normalize_ticker("600519.SS") == "600519"
    assert normalize_ticker("000001.SZ") == "000001"


def test_normalize_ticker_rejects_non_a_share() -> None:
    with pytest.raises(ValueError, match="A-share"):
        normalize_ticker("AAPL")
    with pytest.raises(ValueError, match="A-share"):
        normalize_ticker("0700.HK")


# ---------------------------------------------------------------------------
# HK-ticker parsing
# ---------------------------------------------------------------------------

def test_hk_code_parses_suffix() -> None:
    assert _hk_code("0700.HK") == "00700"
    assert _hk_code("700.HK") == "00700"
    assert _hk_code("9988.HK") == "09988"


def test_hk_code_parses_prefix_and_plain() -> None:
    assert _hk_code("hk00700") == "00700"
    assert _hk_code("00700") == "00700"
    assert _hk_code("9988") == "09988"


def test_hk_code_returns_none_for_non_hk() -> None:
    assert _hk_code("600519") is None
    assert _hk_code("AAPL") is None
    assert _hk_code("sh600519") is None


# ---------------------------------------------------------------------------
# Empty-result fallthrough
# ---------------------------------------------------------------------------

def test_try_sources_skips_empty_results() -> None:
    class _Empty:
        def __len__(self):
            return 0

    calls = []

    def empty():
        calls.append("empty")
        return _Empty()

    def full():
        calls.append("full")
        return "data"

    client = AkshareDataClient()
    assert client._try_sources([empty, full]) == "data"
    assert calls == ["empty", "full"]


# ---------------------------------------------------------------------------
# Tencent HK prices (direct JSON feed — akshare's HK wrappers are broken
# on this network: EastMoney blocked, Sina feed format changed)
# ---------------------------------------------------------------------------

def test_map_hk_tencent_prices() -> None:
    # Tencent returns qfqday as arrays, not dicts
    payload = {
        "code": 0,
        "data": {
            "hk00700": {
                "qfqday": [
                    ["2026-07-02", "345.000", "350.000", "352.000", "343.000", "12345678.000", {}, "0.36", "12345.6"],
                    ["2026-07-03", "349.000", "348.500", "351.000", "346.000", "9876543.000", {}, "-0.40", "9876.5"],
                ]
            }
        },
    }
    prices = _map_hk_tencent_prices(payload, "hk00700")
    assert len(prices) == 2
    assert prices[0].time == "2026-07-02"
    assert prices[0].open == 345.0
    assert prices[0].close == 350.0
    assert prices[0].high == 352.0
    assert prices[0].low == 343.0
    assert prices[0].volume == 12345678
    assert prices[1].close == 348.5


def test_map_hk_tencent_prices_accepts_dict_rows() -> None:
    # Defensive: earlier feeds returned dict rows
    payload = {"data": {"hk00700": {"day": [{"date": "2026-07-02", "open": "1.0",
             "close": "1.1", "high": "1.2", "low": "0.9", "volume": "100"}]}}}
    prices = _map_hk_tencent_prices(payload, "hk00700")
    assert len(prices) == 1 and prices[0].close == 1.1


def test_map_hk_tencent_prices_empty_on_bad_payload() -> None:
    assert _map_hk_tencent_prices({"data": {}}, "hk00700") == []
    assert _map_hk_tencent_prices({}, "hk00700") == []


# ---------------------------------------------------------------------------
# Filing-deadline approximation (point-in-time)
# ---------------------------------------------------------------------------

def test_filing_deadline_q1() -> None:
    assert _filing_deadline("20240331") == "2024-04-30"


def test_filing_deadline_h1() -> None:
    assert _filing_deadline("20240630") == "2024-08-31"


def test_filing_deadline_q3() -> None:
    assert _filing_deadline("20240930") == "2024-10-31"


def test_filing_deadline_annual_is_next_year_april() -> None:
    assert _filing_deadline("20231231") == "2024-04-30"


def test_filing_deadline_accepts_iso_format() -> None:
    # Sina's analysis-indicator feed uses 'YYYY-MM-DD' in the 日期 column
    assert _filing_deadline("2024-03-31") == "2024-04-30"
    assert _filing_deadline("2023-12-31") == "2024-04-30"


def test_filing_deadline_rejects_bad_period() -> None:
    with pytest.raises(ValueError, match="report period"):
        _filing_deadline("20240230")


# ---------------------------------------------------------------------------
# Price mapping (EastMoney daily-hist format)
# ---------------------------------------------------------------------------

def test_map_price_row() -> None:
    row = {
        "日期": "2024-01-02",
        "开盘": 1685.0,
        "收盘": 1690.0,
        "最高": 1700.0,
        "最低": 1680.0,
        "成交量": 3500000,
    }
    price = _map_price(row)
    assert isinstance(price, Price)
    assert price.open == 1685.0
    assert price.close == 1690.0
    assert price.high == 1700.0
    assert price.low == 1680.0
    assert price.volume == 3500000
    assert price.time == "2024-01-02"


def test_map_price_handles_string_numerics() -> None:
    row = {"日期": "2024-01-03", "开盘": "10.5", "收盘": "10.8", "最高": "10.9", "最低": "10.2", "成交量": "123456"}
    price = _map_price(row)
    assert price.open == 10.5
    assert price.close == 10.8
    assert price.volume == 123456


def test_map_price_handles_english_column_names() -> None:
    # Sina/Tencent daily feeds use english column names
    row = {"date": "2024-01-04", "open": 12.5, "close": 12.9, "high": 13.0, "low": 12.4, "volume": 456789}
    price = _map_price(row)
    assert price.open == 12.5
    assert price.close == 12.9
    assert price.high == 13.0
    assert price.low == 12.4
    assert price.volume == 456789
    assert price.time == "2024-01-04"


# ---------------------------------------------------------------------------
# Multi-source fallback (EastMoney -> Sina -> Tencent)
# ---------------------------------------------------------------------------

def test_prefixed_symbol_sh_board() -> None:
    assert _prefixed_symbol("600519") == "sh600519"
    assert _prefixed_symbol("688981") == "sh688981"


def test_prefixed_symbol_sz_board() -> None:
    assert _prefixed_symbol("000001") == "sz000001"
    assert _prefixed_symbol("300750") == "sz300750"


def test_try_sources_returns_first_success() -> None:
    calls = []

    def failing():
        calls.append("fail")
        raise ConnectionError("boom")

    def succeeding():
        calls.append("ok")
        return "data"

    client = AkshareDataClient()
    assert client._try_sources([failing, succeeding]) == "data"
    assert calls == ["fail", "ok"]


def test_try_sources_raises_when_all_fail() -> None:
    def failing():
        raise ConnectionError("boom")

    client = AkshareDataClient()
    with pytest.raises(ConnectionError):
        client._try_sources([failing, failing])


# ---------------------------------------------------------------------------
# Company facts (CNInfo fallback)
# ---------------------------------------------------------------------------

def test_map_company_facts_cninfo_row() -> None:
    row = {
        "A股代码": "600519",
        "A股简称": "贵州茅台",
        "所属市场": "上交所",
        "所属行业": "酒、饮料和精制茶制造业",
    }
    facts = _map_company_facts("600519", row)
    assert isinstance(facts, CompanyFacts)
    assert facts.ticker == "600519"
    assert facts.name == "贵州茅台"
    assert facts.industry == "酒、饮料和精制茶制造业"
    assert facts.exchange == "上交所"


# ---------------------------------------------------------------------------
# Abstract-feed backfill (Sina's wide table fills the margin gap)
# ---------------------------------------------------------------------------

def test_merge_abstract_backfills_missing_margins() -> None:
    metrics = [
        _map_financial_metrics(
            "600519", {"日期": "20260331", "净资产收益率(%)": 10.06}
        )
    ]
    abstract = {
        "20260331": {"毛利率": 89.76, "销售净利率": 52.22},
    }
    merged = _merge_abstract(metrics, abstract)
    assert merged[0].gross_margin == pytest.approx(0.8976)
    assert merged[0].net_margin == pytest.approx(0.5222)
    assert merged[0].return_on_equity == pytest.approx(0.1006)  # untouched


def test_merge_abstract_never_overwrites_existing_values() -> None:
    metrics = [_map_financial_metrics("600519", {"日期": "20240331", "销售毛利率(%)": 91.5})]
    abstract = {"20240331": {"毛利率": 80.0}}
    merged = _merge_abstract(metrics, abstract)
    assert merged[0].gross_margin == pytest.approx(0.915)  # original kept


def test_merge_abstract_accepts_iso_period_key() -> None:
    metrics = [_map_financial_metrics("600519", {"日期": "20251231"})]
    abstract = {"2025-12-31": {"毛利率": 91.18}}
    merged = _merge_abstract(metrics, abstract)
    assert merged[0].gross_margin == pytest.approx(0.9118)


def test_merge_abstract_missing_period_leaves_metrics_alone() -> None:
    metrics = [_map_financial_metrics("600519", {"日期": "20240930"})]
    assert _merge_abstract(metrics, {"20240331": {"毛利率": 91.0}})[0].gross_margin is None


# ---------------------------------------------------------------------------
# Baostock valuation backfill (free peTTM/pbMRQ, point-in-time by date)
# ---------------------------------------------------------------------------

def test_baostock_symbol_sh() -> None:
    assert _baostock_symbol("600519") == "sh.600519"
    assert _baostock_symbol("688981") == "sh.688981"


def test_baostock_symbol_sz() -> None:
    assert _baostock_symbol("000001") == "sz.000001"
    assert _baostock_symbol("300679") == "sz.300679"


def test_match_valuation_latest_on_or_before() -> None:
    rows = [
        ("2026-08-26", "20.1", "6.4"),
        ("2026-08-27", "19.8", "6.4"),
        ("2026-08-28", "19.9", "6.5"),
    ]
    assert _match_valuation(rows, "2026-08-28") == (19.9, 6.5)
    assert _match_valuation(rows, "2026-08-27") == (19.8, 6.4)
    assert _match_valuation(rows, "2026-08-20") is None  # before any row


def test_apply_valuation_fills_pe_pb_but_not_market_cap() -> None:
    metrics = [
        _map_financial_metrics(
            "600519", {"日期": "20260331", "每股净资产_调整前(元)": 150.2}
        )
    ]
    valuation = {"2026-03-31": {"pe": 19.9, "pb": 6.45}}
    _apply_valuation(metrics, valuation)
    assert metrics[0].price_to_earnings_ratio == 19.9
    assert metrics[0].price_to_book_ratio == 6.45
    # Market cap needs a share count the free feeds don't expose; PB x BVPS
    # would be the share PRICE, not the cap — so it stays null.
    assert metrics[0].market_cap is None


def test_apply_valuation_leaves_rows_without_match_alone() -> None:
    metrics = [_map_financial_metrics("600519", {"日期": "20240331"})]
    _apply_valuation(metrics, {})
    assert metrics[0].price_to_earnings_ratio is None


# ---------------------------------------------------------------------------
# Financial-metrics mapping (Sina analysis-indicator format)
# ---------------------------------------------------------------------------

def test_map_financial_metrics_row() -> None:
    # Real Sina column names carry unit suffixes
    row = {
        "日期": "20240331",
        "摊薄每股收益(元)": 18.5,
        "每股净资产_调整前(元)": 150.2,
        "净资产收益率(%)": 12.3,
        "销售毛利率(%)": 91.5,
        "资产负债率(%)": 20.1,
        "流动比率": 3.2,
    }
    m = _map_financial_metrics("600519", row)
    assert isinstance(m, FinancialMetrics)
    assert m.ticker == "600519"
    assert m.report_period == "2024-03-31"
    assert m.period == "ttm"
    assert m.filing_date == "2024-04-30"  # Q1 deadline
    assert m.earnings_per_share == 18.5
    assert m.book_value_per_share == 150.2
    # Percent columns (Sina) are converted to the FD decimal convention
    assert m.return_on_equity == pytest.approx(0.123)
    assert m.gross_margin == pytest.approx(0.915)
    assert m.debt_to_assets == pytest.approx(0.201)
    assert m.current_ratio == 3.2  # ratio multiple, not a percent
    assert m.debt_to_equity is None  # not directly available; left null
    assert m.price_to_earnings_ratio is None  # Sina feed has no PE column
    assert m.price_to_book_ratio is None
    assert m.market_cap is None


def test_map_financial_metrics_missing_fields_stay_null() -> None:
    m = _map_financial_metrics("000001", {"日期": "20231231"})
    assert m.report_period == "2023-12-31"
    assert m.filing_date == "2024-04-30"
    assert m.earnings_per_share is None
    assert m.current_ratio is None


def test_map_financial_metrics_iso_period_column() -> None:
    # Sina returns 'YYYY-MM-DD'; both spellings must map identically
    a = _map_financial_metrics("600519", {"日期": "20240331"})
    b = _map_financial_metrics("600519", {"日期": "2024-03-31"})
    assert a.report_period == b.report_period == "2024-03-31"
    assert a.filing_date == b.filing_date == "2024-04-30"


# ---------------------------------------------------------------------------
# Point-in-time filtering
# ---------------------------------------------------------------------------

def test_metrics_filtered_to_public_by_end_date() -> None:
    rows = [
        {"日期": "20231231"},
        {"日期": "20240331"},
        {"日期": "20240630"},
        {"日期": "20240930"},
    ]
    metrics = [_map_financial_metrics("600519", r) for r in rows]
    # As of 2024-09-01: annual (filed 2024-04-30) and Q1 (2024-04-30) are
    # public; H1 (deadline 2024-08-31) is public; Q3 (deadline 2024-10-31) is not.
    public = [m for m in metrics if m.filing_date <= "2024-09-01"]
    assert [m.report_period for m in public] == ["2023-12-31", "2024-03-31", "2024-06-30"]
