"""Data-client factory — pick the provider (FD vs free A-share) explicitly."""

from __future__ import annotations

import pytest

from hedge_fund.data import AkshareDataClient, FDClient, make_data_client


def test_factory_returns_akshare_client() -> None:
    assert isinstance(make_data_client("akshare"), AkshareDataClient)


def test_factory_returns_fd_client() -> None:
    assert isinstance(make_data_client("fd"), FDClient)


def test_factory_defaults_to_fd(monkeypatch) -> None:
    monkeypatch.delenv("AIHF_DATA_PROVIDER", raising=False)
    assert isinstance(make_data_client(), FDClient)


def test_factory_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("AIHF_DATA_PROVIDER", "akshare")
    assert isinstance(make_data_client(), AkshareDataClient)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown data provider"):
        make_data_client("yahoo")


def test_akshare_client_works_as_context_manager() -> None:
    with AkshareDataClient() as client:
        assert isinstance(client, AkshareDataClient)
