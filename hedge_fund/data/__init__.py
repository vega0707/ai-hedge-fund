"""v2 data pipeline — data provider protocol, FD client, and response models."""

import os

from hedge_fund.data.akshare_client import AkshareDataClient
from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient, FDClientError
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.protocol import DataClient


def make_data_client(provider: str | None = None) -> DataClient:
    """Construct the configured data client.

    ``provider`` wins over the ``AIHF_DATA_PROVIDER`` env var, which wins
    over the default ``fd``. ``akshare`` selects the free A-share client
    (no API key); anything else selects the Financial Datasets client.
    """
    selected = provider or os.environ.get("AIHF_DATA_PROVIDER", "fd").lower()
    if selected == "akshare":
        return AkshareDataClient()
    if selected == "fd":
        return FDClient()
    raise ValueError(f"unknown data provider {selected!r} (expected 'fd' or 'akshare')")


__all__ = [
    "AkshareDataClient",
    "CachedDataClient",
    "CompanyFacts",
    "CompanyNews",
    "DataClient",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "FDClient",
    "FDClientError",
    "Filing",
    "FinancialMetrics",
    "InsiderTrade",
    "Price",
    "make_data_client",
]
