"""Free A-share data client (AkShare) — a DataClient protocol implementation.

No API key, no registration: everything comes from AkShare's free scraping
endpoints (EastMoney / Sina). Implements
``hedge_fund.data.protocol.DataClient`` so the whole v2 pipeline (snapshots,
LLM agents, backtesting) works against A-shares unchanged.

Point-in-time contract
----------------------
``get_financial_metrics`` MUST return only rows public by *end_date* (no
look-ahead). AkShare's analysis-indicator endpoint has no filing date, so
we approximate with the CSRC disclosure deadline: Q1 -> 04-30, H1 -> 08-31,
Q3 -> 10-31, annual -> 04-30 next year. Real filings are never later than
their deadline, so this is a conservative public-by date — a backtest can
never see a period before it was really knowable (it may lag reality by up
to the disclosure window, which biases *against* the strategy, not for it).

Scope limits (fail loudly or stay empty, per protocol):
- Daily bars only; other intervals raise ValueError.
- Insider trades / earnings feeds are not available for free with
  point-in-time guarantees — returned as empty (valid "no data" response).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    FinancialMetrics,
    Price,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure mapping helpers (unit-tested in test_akshare_client.py)
# ---------------------------------------------------------------------------

def normalize_ticker(ticker: str) -> str:
    """Normalize A-share ticker spellings to plain 6 digits.

    Accepts ``600519``, ``sh600519``, ``600519.SH``, ``600519.SS``,
    ``000001.SZ``. Raises ValueError for anything that is not a mainland
    A-share code (e.g. US/HK tickers) — fail loudly rather than silently
    returning garbage to the pipeline.
    """
    t = ticker.strip().lower()
    t = re.sub(r"\.(sh|sz|bj|ss)$", "", t)  # strip exchange suffix
    t = re.sub(r"^(sh|sz|bj)", "", t)        # strip exchange prefix
    if re.fullmatch(r"\d{6}", t):
        return t
    raise ValueError(
        f"{ticker!r} is not an A-share code (expected 6 digits, e.g. 600519)"
    )


def _filing_deadline(report_period: str) -> str:
    """CSRC disclosure deadline for a report period (-> YYYY-MM-DD).

    Accepts both 'YYYYMMDD' and 'YYYY-MM-DD' spellings. Q1 -> 04-30,
    H1 -> 08-31, Q3 -> 10-31, annual -> 04-30 next year.
    """
    compact = report_period.replace("-", "").strip()
    try:
        dt = datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(
            f"invalid report period {report_period!r} (expected YYYYMMDD)"
        ) from exc
    if dt.month == 3 and dt.day == 31:
        return dt.strftime("%Y-04-30")
    if dt.month == 6 and dt.day == 30:
        return dt.strftime("%Y-08-31")
    if dt.month == 9 and dt.day == 30:
        return dt.strftime("%Y-10-31")
    if dt.month == 12 and dt.day == 31:
        return f"{dt.year + 1}-04-30"
    raise ValueError(
        f"report period {report_period!r} is not a quarter end "
        "(03-31 / 06-30 / 09-30 / 12-31)"
    )


def _to_float(value, default=None):
    """Coerce to float; NaN/'-'/''/None all map to *default*."""
    if value is None:
        return default
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if value in ("", "-", "--", "None"):
            return default
    try:
        f = float(value)
        return default if (f != f) else f  # NaN -> default
    except (TypeError, ValueError):
        return default


def _hk_code(ticker: str) -> str | None:
    """Parse a HK ticker to its 5-digit code, or None if not HK.

    Accepts '0700.HK', '700.HK', 'hk00700', '00700', '9988'.
    """
    t = ticker.strip().lower()
    if t.endswith(".hk"):
        digits = t[:-3]
    elif t.startswith("hk"):
        digits = t[2:]
    else:
        digits = t
    if re.fullmatch(r"\d{1,5}", digits):
        return digits.zfill(5)
    return None


def _index_sina_symbol(code: str) -> str | None:
    """Convert A-share index code to Sina index symbol (sh/sz prefix).
    
    Shanghai indices: 000xxx, 950xxx → sh000300
    Shenzhen indices: 399xxx → sz399001
    Returns None if not recognized as an index code.
    """
    if len(code) != 6 or not code.isdigit():
        return None
    if code.startswith(('0', '9')):
        return f'sh{code}'
    if code.startswith('3'):
        return f'sz{code}'
    return None


def _prefixed_symbol(ticker: str) -> str:
    """A-share 6-digit code -> Sina/Tencent symbol ('sh600519', 'sz000001')."""
    code = normalize_ticker(ticker)
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _map_price(row: dict) -> Price:
    """Map a daily-bar row to a Price.

    EastMoney feeds use Chinese column names; Sina/Tencent feeds use
    english ones — both are accepted.
    """
    date = row.get("日期") or row.get("date")
    volume = row.get("成交量") or row.get("volume")
    return Price(
        open=_to_float(row.get("开盘") or row.get("open")),
        close=_to_float(row.get("收盘") or row.get("close")),
        high=_to_float(row.get("最高") or row.get("high")),
        low=_to_float(row.get("最低") or row.get("low")),
        volume=int(_to_float(volume, 0) or 0),
        time=str(date),
    )


_HK_COLUMN_ALIASES: dict[str, list[str]] = {
    "price_to_earnings_ratio": ["市盈率"],
    "price_to_book_ratio": ["市净率"],
    "net_margin": ["销售净利率(%)"],
    "return_on_equity": ["股东权益回报率(%)"],
    "return_on_assets": ["总资产回报率(%)"],
    "revenue_growth": ["营业总收入滚动环比增长(%)"],
    "earnings_per_share": ["基本每股收益(元)"],
    "book_value_per_share": ["每股净资产(元)"],
    "free_cash_flow_per_share": ["每股经营现金流(元)"],
    "market_cap": ["总市值(港元)"],
}

# Field -> candidate Sina column names, tried in order. Real columns carry
# unit suffixes ('净资产收益率(%)'); aliases keep older/sparser feeds working.
_COLUMN_ALIASES: dict[str, list[str]] = {
    # Percent columns -> /100 to the FD decimal convention
    "gross_margin": ["销售毛利率(%)", "销售毛利率"],
    "operating_margin": ["营业利润率(%)", "主营业务利润率(%)", "营业利润率"],
    "net_margin": ["销售净利率(%)", "销售净利率"],
    "return_on_equity": ["净资产收益率(%)", "净资产收益率"],
    "return_on_assets": ["总资产净利润率(%)", "总资产净利润率"],
    "debt_to_assets": ["资产负债率(%)", "资产负债率"],
    "debt_to_equity": ["负债与所有者权益比率(%)", "负债与所有者权益比率"],
    "revenue_growth": ["主营业务收入增长率(%)", "主营业务收入增长率"],
    "earnings_growth": ["净利润增长率(%)", "净利润增长率"],
    "book_value_growth": ["净资产增长率(%)", "净资产增长率"],
    # Ratio multiples / absolute values
    "current_ratio": ["流动比率"],
    "quick_ratio": ["速动比率"],
    "price_to_earnings_ratio": ["市盈率(倍)", "市盈率"],
    "price_to_book_ratio": ["市净率(倍)", "市净率"],
    "market_cap": ["总市值(元)", "总市值"],
    "earnings_per_share": ["摊薄每股收益(元)", "加权每股收益(元)", "每股收益(元)"],
    "book_value_per_share": [
        "每股净资产_调整前(元)", "每股净资产_调整后(元)", "每股净资产(元)",
    ],
    "free_cash_flow_per_share": [
        "每股经营性现金流(元)", "每股经营现金流(元)", "每股经营活动产生的现金流量净额(元)",
    ],
}

_PERCENT_FIELDS = {
    "gross_margin",
    "operating_margin",
    "net_margin",
    "return_on_equity",
    "return_on_assets",
    "debt_to_assets",
    "debt_to_equity",
    "revenue_growth",
    "earnings_growth",
    "book_value_growth",
}


def _column_value(row: dict, aliases: list[str]):
    """First non-empty value among candidate column names."""
    for column in aliases:
        value = row.get(column)
        if _to_float(value) is not None:
            return value
    return None


def _map_financial_metrics(ticker: str, row: dict) -> FinancialMetrics:
    """Map a Sina analysis-indicator row to FinancialMetrics.

    Only the columns Sina exposes are mapped; everything else stays null.
    Percent columns are converted to the FD decimal convention (12.3 -> 0.123)
    so snapshots and prompts are consistent with the US-data path.
    ``filing_date`` is the CSRC disclosure-deadline approximation.
    """
    period = str(row.get("日期", "")).strip()
    compact = period.replace("-", "")
    if not compact or len(compact) != 8:
        raise ValueError("financial metrics row missing report period (日期)")

    values = {
        field: _column_value(row, aliases)
        for field, aliases in _COLUMN_ALIASES.items()
    }
    percent = {
        field: _to_float(values[field]) / 100.0
        for field in _PERCENT_FIELDS
        if _to_float(values.get(field)) is not None
    }
    return FinancialMetrics(
        ticker=ticker,
        report_period=f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}",
        period="ttm",
        filing_date=_filing_deadline(period),
        # Valuation
        market_cap=_to_float(values.get("market_cap")),
        price_to_earnings_ratio=_to_float(values.get("price_to_earnings_ratio")),
        price_to_book_ratio=_to_float(values.get("price_to_book_ratio")),
        # Profitability
        gross_margin=percent.get("gross_margin"),
        operating_margin=percent.get("operating_margin"),
        net_margin=percent.get("net_margin"),
        return_on_equity=percent.get("return_on_equity"),
        return_on_assets=percent.get("return_on_assets"),
        # Liquidity (ratio multiples, not percents)
        current_ratio=_to_float(values.get("current_ratio")),
        quick_ratio=_to_float(values.get("quick_ratio")),
        # Leverage
        debt_to_equity=percent.get("debt_to_equity"),
        debt_to_assets=percent.get("debt_to_assets"),
        # Growth
        revenue_growth=percent.get("revenue_growth"),
        earnings_growth=percent.get("earnings_growth"),
        book_value_growth=percent.get("book_value_growth"),
        # Per-share
        earnings_per_share=_to_float(values.get("earnings_per_share")),
        book_value_per_share=_to_float(values.get("book_value_per_share")),
        free_cash_flow_per_share=_to_float(values.get("free_cash_flow_per_share")),
    )


# ---------------------------------------------------------------------------
# DataClient implementation
# ---------------------------------------------------------------------------

def _map_hk_tencent_prices(payload: dict, code: str) -> list[Price]:
    """Parse Tencent's HK kline JSON into Price rows.

    Tencent's open quote endpoint is a stable public JSON feed (unlike
    akshare's HK wrappers: EastMoney is often blocked, Sina's feed format
    has changed). Rows arrive as arrays — [date, open, close, high, low,
    volume, ...] — with dict rows accepted defensively.
    """
    data = payload.get("data", {})
    if not isinstance(data, dict):
        return []
    block = data.get(code) or {}
    rows = block.get("qfqday") or block.get("day") or []
    prices = []
    for r in rows:
        if isinstance(r, dict):
            if "date" not in r:
                continue
            date, o, c, h, l, v = (
                r.get("date"), r.get("open"), r.get("close"),
                r.get("high"), r.get("low"), r.get("volume"),
            )
        elif isinstance(r, (list, tuple)) and len(r) >= 6:
            date, o, c, h, l, v = r[0], r[1], r[2], r[3], r[4], r[5]
        else:
            continue
        prices.append(
            Price(
                open=_to_float(o),
                close=_to_float(c),
                high=_to_float(h),
                low=_to_float(l),
                volume=int(_to_float(v, 0) or 0),
                time=str(date),
            )
        )
    return prices


def _baostock_symbol(ticker: str) -> str:
    """A-share 6-digit code -> baostock symbol ('sh.600519', 'sz.300679')."""
    code = normalize_ticker(ticker)
    return ("sh" if code.startswith(("6", "9")) else "sz") + "." + code


def _match_valuation(
    rows: list[tuple[str, str, str]], date: str
) -> tuple[float, float] | None:
    """Latest (pe, pb) with row date <= *date* from date-ascending rows.

    baostock peTTM/pbMRQ are daily point-in-time values — the right one
    for a period is the last trading day on or before its filing date.
    """
    best: tuple[str, str] | None = None
    for row_date, pe, pb in rows:
        if row_date <= date:
            best = (pe, pb)
        else:
            break
    if best is None:
        return None
    pe, pb = best
    return _to_float(pe), _to_float(pb)


def _apply_valuation(
    metrics: list[FinancialMetrics],
    valuation: dict[str, dict[str, float]],
) -> list[FinancialMetrics]:
    """Fill price_to_earnings_ratio / price_to_book_ratio from baostock.

    Market cap is NOT derived here: PB x BVPS is the share price, not the
    market cap — a true cap needs a share count, which the free feeds don't
    expose. PE/PB alone let the personas judge valuation.
    """
    for m in metrics:
        v = valuation.get(m.report_period)
        if not v:
            continue
        m.price_to_earnings_ratio = v.get("pe")
        m.price_to_book_ratio = v.get("pb")
    return metrics


def _map_company_facts(ticker: str, row: dict) -> CompanyFacts:
    """Map a CNInfo profile row (stock_profile_cninfo) to CompanyFacts."""
    return CompanyFacts(
        ticker=ticker,
        name=str(row.get("A股简称") or ""),
        industry=str(row.get("所属行业") or ""),
        exchange=str(row.get("所属市场") or ""),
    )


def _merge_abstract(
    metrics: list[FinancialMetrics],
    abstract: dict[str, dict[str, float]],
) -> list[FinancialMetrics]:
    """Backfill gross/net margin from the Sina abstract feed.

    The analysis-indicator feed leaves 销售毛利率(%) NaN for recent periods;
    the abstract wide table (stock_financial_abstract) still carries them.
    Only fills missing values — never overwrites. Abstract keys accept both
    'YYYYMMDD' and 'YYYY-MM-DD' report-period spellings.
    """
    for m in metrics:
        period_data = abstract.get(
            m.report_period.replace("-", "")
        ) or abstract.get(m.report_period)
        if not period_data:
            continue
        if m.gross_margin is None and "毛利率" in period_data:
            m.gross_margin = period_data["毛利率"] / 100.0
        if m.net_margin is None and "销售净利率" in period_data:
            m.net_margin = period_data["销售净利率"] / 100.0
    return metrics


# HK daily bars — Tencent's public quote feed (stable JSON, no token).
_HK_TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"


class AkshareDataClient:
    """Free A-share DataClient. Lazily imports ``akshare`` (heavy dep)."""

    def __init__(self) -> None:
        self._ak = None

    def __enter__(self) -> "AkshareDataClient":
        return self

    def __exit__(self, *args) -> None:
        return None

    # -- protocol: prices -------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
    ) -> list[Price]:
        if interval != "day" or interval_multiplier != 1:
            raise ValueError("AkshareDataClient only supports daily bars")
        ak = self._akshare()
        start, end = start_date.replace("-", ""), end_date.replace("-", "")

        hk = _hk_code(ticker)
        if hk is not None:
            # HK: EastMoney daily (qfq) first, Tencent public JSON as the
            # reliable fallback — akshare's other HK wrappers are broken on
            # some networks (Sina feed format changed).
            sources = [
                lambda: self._fetch(
                    ak.stock_hk_hist, symbol=hk, period="daily",
                    start_date=start, end_date=end, adjust="qfq",
                ),
                lambda: self._fetch_hk_tencent(hk, start, end),
                lambda: self._fetch_baostock_prices(hk, start, end, market="hk"),
            ]
        else:
            symbol = normalize_ticker(ticker)
            # EastMoney (qfq) first, then Sina (qfq), then Tencent (raw) —
            # free scraping sources are flaky and sometimes blocked per-domain.
            # For index codes (000300, 399001), add Sina index feed before
            # EastMoney index (which is often blocked).
            sources = [
                lambda: self._fetch(
                    ak.stock_zh_a_hist, symbol=symbol, period="daily",
                    start_date=start, end_date=end, adjust="qfq",
                ),
                lambda: self._fetch(
                    ak.stock_zh_a_daily, symbol=_prefixed_symbol(symbol),
                    start_date=start, end_date=end, adjust="qfq",
                ),
                lambda: self._fetch(
                    ak.stock_zh_a_hist_tx, symbol=_prefixed_symbol(symbol),
                    start_date=start, end_date=end,
                ),
                lambda: self._fetch_baostock_prices(symbol, start, end),
            ]
            # Add index sources if ticker is an index code
            sina_idx = _index_sina_symbol(symbol)
            if sina_idx:
                sources.append(
                    lambda: self._fetch(
                        ak.stock_zh_index_daily, symbol=sina_idx,
                        start_date=start, end_date=end,
                    )
                )
                sources.append(
                    lambda: self._fetch(
                        ak.index_zh_a_hist, symbol=symbol, period="daily",
                        start_date=start, end_date=end,
                    )
                )
        result = self._try_sources(sources)
        if isinstance(result, list) and result and isinstance(result[0], Price):
            prices = result  # already mapped (Tencent HK path)
        else:
            rows = result.to_dict("records") if hasattr(result, "to_dict") else (result or [])
            prices = [_map_price(r) for r in rows]
        return [p for p in prices if start_date <= p.time <= end_date]

    # -- protocol: financial metrics (point-in-time) ----------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        hk = _hk_code(ticker)
        if hk is not None:
            return self._fetch_hk_financial_metrics(hk, ticker, end_date, limit)
        symbol = normalize_ticker(ticker)
        ak = self._akshare()
        df = self._fetch(ak.stock_financial_analysis_indicator, symbol=symbol)
        metrics = [_map_financial_metrics(symbol, r) for r in df.to_dict("records")]
        metrics = _merge_abstract(metrics, self._fetch_abstract(ak, symbol))
        metrics = _apply_valuation(metrics, self._fetch_baostock_valuation(symbol, metrics, end_date))
        # Newest first; only rows provably public by end_date (no look-ahead).
        metrics.sort(key=lambda m: m.filing_date or "", reverse=True)
        return [m for m in metrics if m.filing_date and m.filing_date <= end_date][:limit]

    def _fetch_hk_financial_metrics(
        self, hk_code: str, ticker: str, end_date: str, limit: int,
    ) -> list[FinancialMetrics]:
        """HK stock financial metrics from EastMoney (AkShare).

        Uses ``stock_hk_financial_indicator_em`` which returns the latest
        snapshot of key ratios (PE, PB, market cap, EPS, ROE, net margin, …)
        per report period.  Each row = one reporting period, so we get the
        same shape as the A-share path.

        Gross/operating margin are NOT exposed by this endpoint — those
        fields stay null.  The persona agents tolerate partial data and
        will judge on whatever is present.
        """
        ak = self._akshare()
        df = self._fetch(ak.stock_hk_financial_indicator_em, symbol=hk_code)
        if df.empty:
            return []

        metrics: list[FinancialMetrics] = []
        for _, row in df.iterrows():
            report_period = str(row.get("日期", "")).strip()
            # EastMoney HK rows carry the report date in a "报告期" column
            # for the indicator endpoint; fall back to 日期 if absent.
            if not report_period or report_period == "nan":
                report_period = str(row.get("报告期", "")).strip()
            if not report_period:
                continue
            # Normalise YYYYMMDD / YYYY-MM-DD → YYYY-MM-DD
            compact = report_period.replace("-", "").strip()
            if len(compact) != 8:
                continue
            rp = f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
            if rp > end_date:
                continue

            values = {
                field: _column_value(row, aliases)
                for field, aliases in _HK_COLUMN_ALIASES.items()
            }
            percent = {
                f: _to_float(values[f]) / 100.0
                for f in ("net_margin", "return_on_equity", "return_on_assets",
                          "revenue_growth")
                if _to_float(values.get(f)) is not None
            }

            metrics.append(FinancialMetrics(
                ticker=ticker,
                report_period=rp,
                period="annual",
                currency="HKD",
                # Valuation
                market_cap=_to_float(values.get("market_cap")),
                price_to_earnings_ratio=_to_float(values.get("price_to_earnings_ratio")),
                price_to_book_ratio=_to_float(values.get("price_to_book_ratio")),
                # Profitability
                net_margin=percent.get("net_margin"),
                return_on_equity=percent.get("return_on_equity"),
                return_on_assets=percent.get("return_on_assets"),
                # Per-share
                earnings_per_share=_to_float(values.get("earnings_per_share")),
                book_value_per_share=_to_float(values.get("book_value_per_share")),
                free_cash_flow_per_share=_to_float(values.get("free_cash_flow_per_share")),
            ))

        metrics.sort(key=lambda m: m.report_period, reverse=True)
        return metrics[:limit]

    def _fetch_hk_tencent(self, hk_code: str, start: str, end: str) -> list[Price]:
        """HK daily bars from Tencent's public kline JSON endpoint.

        ``start``/``end`` arrive as YYYYMMDD; the feed wants YYYY-MM-DD.
        """
        symbol = f"hk{hk_code}"
        s = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
        e = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
        params = {"param": f"{symbol},day,{s},{e},640,qfq"}
        resp = requests.get(_HK_TENCENT_URL, params=params, timeout=30)
        resp.raise_for_status()
        return _map_hk_tencent_prices(resp.json(), symbol)

    def _baostock_prices_symbol(self, code: str, market: str = "a") -> str:
        """Code -> baostock symbol for price queries.

        A-share: ``600519`` -> ``sh.600519``; HK: ``01810`` -> ``hk.01810``.
        """
        if market == "hk":
            return f"hk.{code}"
        return _baostock_symbol(code)

    def _fetch_baostock_prices(
        self, code: str, start: str, end: str, market: str = "a",
    ) -> list[Price]:
        """Daily OHLCV from baostock — used as the last-resort price source.

        baostock covers A-shares (``sh.``/``sz.``) and HK (``hk.``) with the
        same ``query_history_k_data_plus`` call. A login/logout cycle is
        cheap (~0.3s) and we already know it works when AkShare's scraper
        endpoints are blocked.
        """
        # start/end arrive as YYYYMMDD from get_prices; baostock wants YYYY-MM-DD.
        s = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
        e = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
        symbol = self._baostock_prices_symbol(code, market)
        try:
            import baostock as bs
            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"baostock login failed: {lg.error_msg}")
            try:
                rs = bs.query_history_k_data_plus(
                    symbol, "date,open,high,low,close,volume",
                    start_date=s, end_date=e,
                    frequency="d", adjustflag="2",  # 前复权
                )
                if rs.error_code != "0":
                    raise RuntimeError(f"baostock query failed: {rs.error_msg}")
                rows = []
                while rs.next():
                    d = rs.get_row_data()
                    # d = [date, open, high, low, close, volume]
                    if d and d[0] and all(v != "" for v in d[1:5]):
                        rows.append(Price(
                            open=_to_float(d[1]),
                            high=_to_float(d[2]),
                            low=_to_float(d[3]),
                            close=_to_float(d[4]),
                            volume=int(float(d[5] or 0)),
                            time=d[0],
                        ))
            finally:
                bs.logout()
        except Exception as exc:
            logger.warning("baostock prices unavailable for %s: %s", symbol, exc)
            raise
        return rows

    def _fetch_baostock_valuation(
        self,
        symbol: str,
        metrics: list[FinancialMetrics],
        end_date: str,
    ) -> dict[str, dict[str, float]]:
        """Free daily peTTM/pbMRQ from baostock, matched to each filing date.

        One range query covers all periods; rows before any filing date are
        ignored (point-in-time). A baostock outage must not take down the
        fundamentals — empty map, metrics keep their nulls.
        """
        filing_dates = [m.filing_date for m in metrics if m.filing_date]
        if not filing_dates:
            return {}
        bs_symbol = _baostock_symbol(symbol)  # 600519 -> sh.600519
        try:
            import baostock as bs
            lg = bs.login()
            if lg.error_code != "0":
                logger.warning("baostock login failed: %s", lg.error_msg)
                return {}
            try:
                rs = bs.query_history_k_data_plus(
                    bs_symbol, "date,peTTM,pbMRQ",
                    start_date=min(filing_dates), end_date=end_date,
                    frequency="d", adjustflag="3",
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    d = rs.get_row_data()
                    if d and d[0] and d[1] != "" and d[2] != "":
                        rows.append((d[0], d[1], d[2]))
            finally:
                bs.logout()
        except Exception as exc:  # noqa: BLE001 — optional enrichment
            logger.warning("baostock valuation unavailable for %s: %s", symbol, exc)
            return {}
        rows.sort()
        return {
            m.report_period: {"pe": pe, "pb": pb}
            for m in metrics
            if (matched := _match_valuation(rows, m.filing_date or "")) is not None
            for pe, pb in [matched]
            if pe is not None or pb is not None
        }

    def _fetch_abstract(self, ak, symbol: str) -> dict[str, dict[str, float]]:
        """Sina abstract wide table -> {report_period: {metric: value}}.

        Only the margin rows the main feed is missing are kept. The feed is
        a nicety — if it fails, metrics simply keep their nulls.
        """
        try:
            df = self._fetch(ak.stock_financial_abstract, symbol=symbol)
        except Exception as exc:  # noqa: BLE001 — optional enrichment
            logger.warning("abstract feed unavailable for %s: %s", symbol, exc)
            return {}
        out: dict[str, dict[str, float]] = {}
        for _, row in df.iterrows():
            name = str(row.get("指标") or "")
            if name not in {"毛利率", "销售净利率"}:
                continue
            for column in df.columns[2:]:  # report-period columns
                value = row.get(column)
                if value is None or value != value:  # None or NaN
                    continue
                out.setdefault(str(column), {})[name] = float(value)
        return out

    # -- protocol: news ---------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        symbol = normalize_ticker(ticker)
        ak = self._akshare()
        df = self._fetch(ak.stock_news_em, symbol=symbol)
        news: list[CompanyNews] = []
        for r in df.to_dict("records"):
            date = str(r.get("发布时间") or "")[:10]
            if not date or date > end_date:
                continue
            if start_date and date < start_date:
                continue
            news.append(
                CompanyNews(
                    ticker=symbol,
                    title=str(r.get("新闻标题") or ""),
                    source=str(r.get("文章来源") or ""),
                    date=date,
                    url=str(r.get("新闻链接") or ""),
                )
            )
            if len(news) >= limit:
                break
        return news

    # -- protocol: company facts ------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        symbol = normalize_ticker(ticker)
        ak = self._akshare()
        # EastMoney profile first, CNInfo as fallback (EastMoney is the
        # flaky domain). Both missing -> None: company metadata is a slow
        # variable, not a signal — absence degrades the snapshot's
        # sector/industry lines, never the decision.
        sources = [
            lambda: self._fetch(ak.stock_individual_info_em, symbol=symbol),
            lambda: self._fetch(ak.stock_profile_cninfo, symbol=symbol),
        ]
        try:
            df = self._try_sources(sources)
        except Exception as exc:  # noqa: BLE001 — non-critical metadata
            logger.warning("company facts unavailable for %s: %s", symbol, exc)
            return None
        rows = df.to_dict("records")
        if not rows:
            return None
        row = rows[0]
        # EastMoney shape: item/value pairs; CNInfo shape: flat record.
        if "item" in row and "value" in row:
            info = dict(zip(df["item"], df["value"]))
            return CompanyFacts(
                ticker=symbol,
                name=str(info.get("股票简称") or info.get("股票名称") or ""),
                industry=str(info.get("行业") or ""),
                exchange=str(info.get("交易所") or ""),
            )
        return _map_company_facts(symbol, row)

    # -- protocol: feeds without a free point-in-time source --------------

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list:
        # No free holder-change feed with point-in-time guarantees; an empty
        # list is a valid DataClient "no data" response.
        return []

    def get_earnings(self, ticker: str) -> None:
        return None

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list:
        return []

    # -- protocol: convenience --------------------------------------------

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        metrics = self.get_financial_metrics(ticker, end_date, limit=1)
        return metrics[0].market_cap if metrics else None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _akshare(self):
        if self._ak is None:
            import akshare  # heavy; import on first use
            self._ak = akshare
        return self._ak

    def _fetch(self, fn, *args, retries: int = 3, delay: float = 2.0, **kwargs):
        """Call ``fn(*args, **kwargs)`` retrying transient failures.

        AkShare scrapes public Chinese-finance endpoints that routinely
        drop connections; a bounded retry is the pragmatic fix. After
        exhausting retries the last exception propagates — infrastructure
        failures stay loud, they never become empty data.
        """
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — any source error is retryable
                last = exc
                logger.warning(
                    "akshare call failed (attempt %d/%d): %s",
                    attempt + 1, retries, exc,
                )
                if attempt < retries - 1:
                    time.sleep(delay)
        assert last is not None
        raise last

    def _try_sources(self, sources: list) -> object:
        """Return the first source that yields non-empty data; else raise.

        Each source is a zero-arg callable (a closure over a single AkShare
        endpoint). Failures fall through to the next source — a per-domain
        block on one scraper must not take down the pipeline. A result that
        is None or empty (len 0) also falls through: for benchmark index
        codes the stock feeds return empty and the index feed is the real
        source.
        """
        last: Exception | None = None
        for source in sources:
            try:
                result = source()
            except Exception as exc:  # noqa: BLE001 — fall through, next source
                last = exc
                logger.warning("price source failed: %s", exc)
                continue
            if result is not None and len(result) > 0:
                return result
        if last is not None:
            raise last
        raise ValueError("all price sources returned no data")
