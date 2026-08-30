"""Point-in-time fundamentals snapshot — the shared input for LLM analysts.

A `FundamentalsSnapshot` is everything an investor agent is allowed to know
about a company as of a given date: a history of financial metrics (each row
provably public by `as_of` — the data layer filters on filing_date, not
report_period) plus a few derived aggregates computed here in Python so the
LLM reasons over facts instead of re-deriving arithmetic.

The snapshot is pure data: build it once, hash it, feed it to any persona.
`content_hash` is the cache key for LLM calls — an agent only re-reasons
when a new filing changes its snapshot. Both the hash and `render()` exclude
`as_of`: two dates between filings see identical data, and identical data
must produce an identical prompt (a cache hit), not two paid LLM calls.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from hedge_fund.data.protocol import DataClient

# An agent can't say anything defensible about a company with less history
# than this (one year of ttm rows).
MIN_PERIODS = 4


class InsufficientData(ValueError):
    """Not enough point-in-time history to build a snapshot."""


class PeriodFundamentals(BaseModel):
    """One reporting period's key metrics, compacted for prompting."""

    report_period: str
    filing_date: str | None = None
    market_cap: float | None = None
    price_to_earnings_ratio: float | None = None
    return_on_equity: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    revenue_growth: float | None = None
    earnings_per_share: float | None = None
    book_value_per_share: float | None = None
    free_cash_flow_per_share: float | None = None


class FundamentalsSnapshot(BaseModel):
    """What an analyst may know about *ticker* as of *as_of*. Newest first."""

    ticker: str
    as_of: str
    sector: str | None = None
    industry: str | None = None
    periods: list[PeriodFundamentals]

    # Derived aggregates (computed in build_snapshot, not by the LLM)
    roe_avg: float | None = None
    net_margin_avg: float | None = None
    gross_margin_trend: float | None = None  # latest minus oldest
    bvps_cagr: float | None = None
    debt_to_equity_latest: float | None = None
    market_cap_latest: float | None = None

    @property
    def content_hash(self) -> str:
        """Stable hash of the fundamentals content — the LLM cache key.

        Excludes `as_of` so two dates between filings share one hash: an
        unchanged snapshot must be free, not a fresh LLM call per date.
        """
        canonical = self.model_dump_json(exclude={"as_of"})
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def render(self) -> str:
        """Compact text block for the LLM prompt.

        Deliberately date-free (no `as_of`): the prompt cache keys on exact
        prompt text, so the same fundamentals must render identically on any
        date. It also keeps the LLM from anchoring on a calendar date it
        could associate with post-date world events.
        """
        lines = [
            f"公司：{self.ticker}"
            + (f"  |  行业板块：{self.sector}" if self.sector else "")
            + (f"  |  所属行业：{self.industry}" if self.industry else ""),
            "以下所有数据均已按披露日期在相应时点公开。请把最近一次披露视为当前时点。",
            "",
            "摘要：",
            f"  市值（最新披露）：{_fmt(self.market_cap_latest)}",
            f"  平均ROE：{_fmt(self.roe_avg)}  |  平均净利率：{_fmt(self.net_margin_avg)}",
            f"  毛利率趋势（最新-最早）：{_fmt(self.gross_margin_trend)}",
            f"  每股账面价值年化增长：{_fmt(self.bvps_cagr)}",
            f"  负债/权益（最新）：{_fmt(self.debt_to_equity_latest)}",
            "",
            "历史（滚动十二个月期，最新在前）：",
            "报告期 | 披露日 | 市值 | 市盈率 | ROE | 毛利率 | 营业利润率 | "
            "净利率 | 负债/权益 | 流动比率 | 营收增速 | 每股收益 | 每股净资产 | 每股现金流",
        ]
        for p in self.periods:
            lines.append(
                f"{p.report_period} | {p.filing_date or '?'} | {_fmt(p.market_cap)} "
                f"| {_fmt(p.price_to_earnings_ratio)} | {_fmt(p.return_on_equity)} "
                f"| {_fmt(p.gross_margin)} | {_fmt(p.operating_margin)} "
                f"| {_fmt(p.net_margin)} | {_fmt(p.debt_to_equity)} "
                f"| {_fmt(p.current_ratio)} | {_fmt(p.revenue_growth)} "
                f"| {_fmt(p.earnings_per_share)} | {_fmt(p.book_value_per_share)} "
                f"| {_fmt(p.free_cash_flow_per_share)}"
            )
        return "\n".join(lines)


def build_snapshot(
    ticker: str,
    as_of: str,
    data_client: DataClient,
    periods: int = 20,
) -> FundamentalsSnapshot:
    """Build the point-in-time snapshot for (ticker, as_of).

    Raises InsufficientData if fewer than MIN_PERIODS filed periods exist.
    Data-layer failures propagate (fail loud) — a broken snapshot must never
    silently become a neutral view.
    """
    metrics = data_client.get_financial_metrics(
        ticker, as_of, period="ttm", limit=periods,
    )
    if len(metrics) < MIN_PERIODS:
        raise InsufficientData(
            f"{ticker} as of {as_of}: only {len(metrics)} filed periods "
            f"(need {MIN_PERIODS})"
        )

    # Market cap comes from the most recent FILED metrics row. Deliberately
    # NOT data_client.get_market_cap(): that prefers company_facts.market_cap,
    # which is latest-only — lookahead in a backtest.
    facts = data_client.get_company_facts(ticker)

    rows = [
        PeriodFundamentals(**m.model_dump(include=set(PeriodFundamentals.model_fields)))
        for m in metrics
    ]

    return FundamentalsSnapshot(
        ticker=ticker,
        as_of=as_of,
        # Sector/industry are slow-moving company attributes; using latest
        # facts here is an accepted, documented PIT approximation.
        sector=facts.sector if facts else None,
        industry=facts.industry if facts else None,
        periods=rows,
        roe_avg=_avg([m.return_on_equity for m in metrics]),
        net_margin_avg=_avg([m.net_margin for m in metrics]),
        gross_margin_trend=_trend([m.gross_margin for m in metrics]),
        bvps_cagr=_cagr([m.book_value_per_share for m in metrics]),
        debt_to_equity_latest=metrics[0].debt_to_equity,
        market_cap_latest=metrics[0].market_cap,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:.2f}"


def _avg(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _trend(values: list[float | None]) -> float | None:
    """Latest minus oldest (values arrive newest first)."""
    xs = [v for v in values if v is not None]
    return round(xs[0] - xs[-1], 4) if len(xs) >= 2 else None


def _cagr(values: list[float | None]) -> float | None:
    """Annualized growth from oldest to latest (ttm rows are quarter-spaced)."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2 or xs[-1] is None or xs[-1] <= 0 or xs[0] <= 0:
        return None
    years = (len(xs) - 1) / 4  # quarter-spaced ttm periods
    if years <= 0:
        return None
    return round((xs[0] / xs[-1]) ** (1 / years) - 1, 4)
