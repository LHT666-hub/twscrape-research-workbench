import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from research_workbench.core import Hold, Transient
from research_workbench.transport import FixedAccountTransport, strict_rows
from twscrape.account import Account
from twscrape.accounts_pool import AccountsPool
from twscrape.http import NetworkError
from twscrape.queue_client import XClIdGenStore


class FakeHTTP:
    def __init__(self, status=200, error=None):
        self.status, self.error = status, error
        self.calls = []
        self.closed = False

    async def request(self, method, url, params, headers):
        self.calls.append((method, url, params, headers))
        if self.error:
            raise self.error
        body = {
            "data": {
                "search_by_raw_query": {
                    "search_timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineReplaceEntry",
                                    "entry": {"content": {"cursorType": "Bottom", "value": "next"}},
                                }
                            ]
                        }
                    }
                }
            }
        }
        return SimpleNamespace(
            status_code=self.status,
            headers={},
            url=url + "?" + urlencode(params),
            json=lambda: body,
            text="",
        )

    async def aclose(self):
        self.closed = True


async def mock_signer(*args, **kwargs):
    return SimpleNamespace(calc=lambda *args: "mocked-transaction-id")


@pytest.mark.parametrize("status", [200, 401, 403, 404, 429])
@pytest.mark.asyncio
async def test_direct_adapter_preserves_all_statuses_without_rotation(
    tmp_path, monkeypatch, status
):
    database = tmp_path / "accounts.db"
    pool = AccountsPool(str(database))
    await pool.add_account_cookies("test", "auth_token=dummy_test; ct0=dummy_csrf")
    http = FakeHTTP(status)
    monkeypatch.setattr(Account, "make_client", lambda *args, **kwargs: http)
    monkeypatch.setattr(XClIdGenStore, "get", mock_signer)
    async with FixedAccountTransport(database, "test") as transport:
        page = await transport.fetch("gpt exact-query", "saved-cursor")
    assert page.status == status and page.query == "gpt exact-query"
    assert page.request_cursor == "saved-cursor" and page.rows == []
    assert len(http.calls) == 1 and http.closed
    variables = json.loads(http.calls[0][2]["variables"])
    assert variables["cursor"] == "saved-cursor" and variables["product"] == "Latest"


@pytest.mark.asyncio
async def test_multiple_accounts_forbidden_before_any_network(tmp_path):
    database = tmp_path / "accounts.db"
    pool = AccountsPool(str(database))
    for name in ["one", "two"]:
        await pool.add_account_cookies(name, "auth_token=dummy_test; ct0=dummy_csrf")
    with pytest.raises(Hold):
        async with FixedAccountTransport(database, "one"):
            pytest.fail("Must not enter a rotating account pool")


@pytest.mark.asyncio
async def test_transport_error_converted_to_bounded_network_retry(tmp_path, monkeypatch):
    database = tmp_path / "accounts.db"
    await AccountsPool(str(database)).add_account_cookies(
        "test", "auth_token=dummy_test; ct0=dummy_csrf"
    )
    http = FakeHTTP(error=NetworkError("fixture-network-failure"))
    monkeypatch.setattr(Account, "make_client", lambda *args, **kwargs: http)
    monkeypatch.setattr(XClIdGenStore, "get", mock_signer)
    async with FixedAccountTransport(database, "test") as transport:
        with pytest.raises(Transient):
            await transport.fetch("gpt", "cursor")
    assert len(http.calls) == 1


def test_strict_parser_exposes_failures_and_passive_location(monkeypatch):
    import research_workbench.transport as module

    monkeypatch.setattr(
        module,
        "to_old_rep",
        lambda body: {
            "tweets": {"1": {"id_str": "1"}, "2": {"id_str": "2"}, "3": {"id_str": "3"}},
            "quoted_ids": {"3"},
            "retweeted_ids": set(),
        },
    )

    def parse(raw, context):
        if raw["id_str"] == "2":
            raise ValueError("fixture")
        return SimpleNamespace(
            id=1,
            date=datetime(2026, 6, 25, tzinfo=timezone.utc),
            rawContent="gpt",
            url="https://x.com/example/status/1",
            user=SimpleNamespace(username="example", location="Self-reported fixture"),
        )

    monkeypatch.setattr(module.Tweet, "parse", parse)
    rows, errors = strict_rows({})
    assert errors == 1 and len(rows) == 1
    assert rows[0]["profile_location"] == "Self-reported fixture"
