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
    _filing_deadline,
    _map_company_facts,
    _map_financial_metrics,
    _map_price,
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
