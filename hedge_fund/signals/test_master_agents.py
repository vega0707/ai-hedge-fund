"""New master-strategy models: registration + Greenblatt quant logic.

The LLM personas are contract-checked by the shared registry tests in
test_llm_agents.py (name == key, PIT hard rule, JSON schema). Here we pin
the additions specifically: the new keys exist, and the one pure-quant
addition (Magic Formula) reasons correctly over point-in-time metrics.
"""

from __future__ import annotations

import pytest

from hedge_fund.data.models import FinancialMetrics
from hedge_fund.models import Signal
from hedge_fund.signals import ALPHA_MODEL_REGISTRY, GreenblattModel, LLMAgent

NEW_MASTER_PERSONAS = ["burry", "marks", "dalio", "ackman", "templeton", "neff"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_new_master_personas_registered() -> None:
    for key in NEW_MASTER_PERSONAS:
        cls = ALPHA_MODEL_REGISTRY.get(key)
        assert cls is not None, f"{key} missing from ALPHA_MODEL_REGISTRY"
        assert issubclass(cls, LLMAgent)


def test_greenblatt_registered_as_quant_model() -> None:
    cls = ALPHA_MODEL_REGISTRY.get("greenblatt")
    assert cls is not None
    assert not issubclass(cls, LLMAgent)  # pure math, no LLM cost


# ---------------------------------------------------------------------------
# Greenblatt (Magic Formula) quant logic
# ---------------------------------------------------------------------------

class FakeDataClient:
    def __init__(self, metrics: list[FinancialMetrics], prices: list | None = None) -> None:
        self._metrics = metrics
        self._prices = prices or []

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
        return self._metrics

    def get_prices(self, ticker, start_date, end_date, **kwargs):
        return self._prices


class FakePrice:
    def __init__(self, close: float, time: str = "2025-01-10"):
        self.close = close
        self.time = time


def _history(roe: float, pe: float | None, n: int = 8, eps: float | None = None) -> list[FinancialMetrics]:
    quarters = ["2024-12-31", "2024-09-30", "2024-06-30", "2024-03-31",
                "2023-12-31", "2023-09-30", "2023-06-30", "2023-03-31"]
    return [
        FinancialMetrics(
            ticker="600519", report_period=q, period="ttm", filing_date=q,
            return_on_equity=roe, price_to_earnings_ratio=pe,
            earnings_per_share=eps, market_cap=1e12, gross_margin=0.9,
        )
        for q in quarters[:n]
    ]


def test_greenblatt_bullish_on_high_roe_low_pe() -> None:
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(_history(roe=0.25, pe=10.0)))
    assert isinstance(sig, Signal)
    assert sig.value > 0.0
    assert sig.model_name == "greenblatt"


def test_greenblatt_bearish_on_low_roe_high_pe() -> None:
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(_history(roe=0.05, pe=50.0)))
    assert sig.value < 0.0


def test_greenblatt_neutral_on_quality_without_value() -> None:
    # Great ROE but rich price: Magic Formula says wait, not buy.
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(_history(roe=0.20, pe=25.0)))
    assert sig.value == 0.0


def test_greenblatt_neutral_on_insufficient_history() -> None:
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(_history(roe=0.25, pe=10.0, n=1)))
    assert sig.value == 0.0


def test_greenblatt_derives_ey_from_eps_and_price() -> None:
    # A-share free feed has no PE column: EY must come from EPS / price.
    # EPS 1.5, price 10 -> EY 0.15 >= bar, ROE 0.25 >= bar -> bullish.
    metrics = _history(roe=0.25, pe=None, eps=1.5)
    prices = [FakePrice(close=10.0)]
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(metrics, prices))
    assert sig.value > 0.0
    assert sig.reasoning and "盈利收益率" in sig.reasoning


def test_greenblatt_derived_ey_bearish_when_price_too_rich() -> None:
    # EPS 1.5, price 60 -> EY 0.025 below the low bar -> bearish.
    metrics = _history(roe=0.20, pe=None, eps=1.5)
    prices = [FakePrice(close=60.0)]
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(metrics, prices))
    assert sig.value < 0.0


def test_greenblatt_neutral_when_no_price_or_eps() -> None:
    metrics = _history(roe=0.25, pe=None)
    sig = GreenblattModel().predict("600519", "2025-01-15", FakeDataClient(metrics, []))
    assert sig.value == 0.0
