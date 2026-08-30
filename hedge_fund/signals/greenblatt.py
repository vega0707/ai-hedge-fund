"""Joel Greenblatt magic-formula model — quality at a discount, in pure math.

The Magic Formula ranks businesses by return on capital (quality) and
earnings yield (value). Without a cross-section of names the rank-based
formula is approximated with fixed bars: buy when average ROE is strong AND
the price is cheap (high earnings yield), avoid when either is poor.

QuantModel — no LLM cost, runs in backtests for free.
"""

from __future__ import annotations

from hedge_fund.data.protocol import DataClient
from hedge_fund.models import Signal
from hedge_fund.signals.base import QuantModel

_MIN_PERIODS = 2          # need at least two filed periods to trust ROE
_MIN_ROE = 0.15           # quality bar (FD decimal convention, 15%)
_MIN_EY = 1.0 / 16.7      # value bar: P/E <= ~16.7
_LOW_ROE = 0.075          # below this the business is failing the test
_LOW_EY = 1.0 / 33.0      # below this the price is a value trap


class GreenblattModel(QuantModel):
    """Magic Formula: high average ROE plus high earnings yield.

    Earnings yield is approximated as 1/P/E (EBIT/EV is not exposed by the
    free data path). Only point-in-time metrics (filing_date <= date) are
    used — no look-ahead.
    """

    @property
    def name(self) -> str:
        return "greenblatt"

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        metrics = data_client.get_financial_metrics(ticker, date, period="ttm", limit=8)
        roes = [m.return_on_equity for m in metrics if m.return_on_equity is not None]
        eys = [
            1.0 / m.price_to_earnings_ratio
            for m in metrics
            if m.price_to_earnings_ratio is not None and m.price_to_earnings_ratio > 0
        ]
        if not eys:
            # A-share free feed has no PE column — derive EY from EPS / price
            eys = self._derived_earnings_yields(ticker, date, data_client, metrics)
        if len(roes) < _MIN_PERIODS or not eys:
            return self._neutral(ticker, date, "数据不足")

        roe_avg = sum(roes) / len(roes)
        ey = min(eys)  # most conservative (cheapest) observed multiple

        if roe_avg >= _MIN_ROE and ey >= _MIN_EY:
            value = self._normalize_to_signal((roe_avg - _MIN_ROE) * 5 + (ey - _MIN_EY) * 10)
            return Signal(
                model_name=self.name,
                ticker=ticker,
                date=date,
                value=value,
                reasoning=(
                    f"魔法公式：平均ROE {roe_avg:.1%}，盈利收益率 {ey:.1%} "
                    f"（对应最低市盈率 {1.0 / ey:.1f}）"
                ),
                components={"roe_avg": roe_avg, "earnings_yield": ey},
            )
        if roe_avg < _LOW_ROE or ey < _LOW_EY:
            value = self._normalize_to_signal((roe_avg - _MIN_ROE) * 5 + (ey - _MIN_EY) * 10)
            return Signal(
                model_name=self.name,
                ticker=ticker,
                date=date,
                value=value,
                reasoning=(
                    f"魔法公式不满足：平均ROE {roe_avg:.1%}，"
                    f"盈利收益率 {ey:.1%}"
                ),
                components={"roe_avg": roe_avg, "earnings_yield": ey},
            )
        return self._neutral(ticker, date, "好公司但不便宜（或便宜但质量差）")

    def _derived_earnings_yields(
        self,
        ticker: str,
        date: str,
        data_client: DataClient,
        metrics: list,
    ) -> list[float]:
        """Earnings yield from EPS / latest price when the feed has no PE.

        Only prices with time <= date are used (point-in-time). An empty
        return means "no view", not an error — the price path is optional.
        """
        try:
            prices = data_client.get_prices(ticker, "1900-01-01", date)
        except Exception:  # noqa: BLE001 — optional path
            return []
        if not prices:
            return []
        price = prices[-1].close
        if not price or price <= 0:
            return []
        return [
            m.earnings_per_share / price
            for m in metrics
            if m.earnings_per_share is not None and m.earnings_per_share > 0
        ]

    def _neutral(self, ticker: str, date: str, reason: str) -> Signal:
        return Signal(
            model_name=self.name,
            ticker=ticker,
            date=date,
            value=0.0,
            reasoning=f"中性：{reason}",
        )
