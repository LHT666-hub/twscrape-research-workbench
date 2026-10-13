"""Reuse twscrape's account/signing/HTTP/parser primitives, not its end heuristics.

Exactly one isolated account. No account rotation, browser-profile reads, About
requests, implicit 404 retries, or dropping empty/error responses.
"""

import json
from urllib.parse import parse_qs, urlsplit

from twscrape.accounts_pool import AccountsPool
from twscrape.api import GQL_FEATURES, GQL_URL, OP_SearchTimeline
from twscrape.models import Tweet
from twscrape.queue_client import QueueClient, XClIdGenStore
from twscrape.utils import encode_params, to_old_rep

from .core import TZ, Hold, Page, Transient


def strict_rows(body):
    normalized = to_old_rep(body)
    ignored = normalized.get("retweeted_ids", set()) | normalized.get("quoted_ids", set())
    rows, errors = [], 0
    for raw in normalized["tweets"].values():
        if raw.get("id_str") in ignored:
            continue
        try:
            tweet = Tweet.parse(raw, normalized)
            rows.append(
                {
                    "post_id": str(tweet.id),
                    "posted_at": tweet.date.astimezone(TZ).isoformat(),
                    "post_text": tweet.rawContent,
                    "post_url": tweet.url,
                    "author_username": tweet.user.username,
                    "profile_location": tweet.user.location or "",
                }
            )
        except Exception:
            errors += 1
    return rows, errors


class FixedAccountTransport:
    def __init__(self, database, account):
        self.database, self.account = database, account

    async def __aenter__(self):
        self.pool = AccountsPool(str(self.database), raise_when_no_account=True)
        accounts = await self.pool.get_all()
        if len(accounts) != 1 or accounts[0].username != self.account or not accounts[0].active:
            raise Hold(
                "account_configuration",
                "Pilot database must contain exactly the selected active account",
            )
        self.client = QueueClient(self.pool, "SearchTimeline", debug=False)
        await self.client.__aenter__()
        if self.client.ctx is None or self.client.ctx.acc.username != self.account:
            await self.client.__aexit__(None, None, None)
            raise Hold("account_configuration", "Selected account unavailable; no rotation")
        return self

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)

    async def fetch(self, query, cursor):
        ctx = self.client.ctx
        if ctx is None:
            raise Hold("authentication_hold", "Account context absent")
        url = f"{GQL_URL}/{OP_SearchTimeline}"
        generator = await XClIdGenStore.get(
            ctx.acc.username, proxy=ctx.proxy, cookies=ctx.acc.cookies
        )
        variables = {
            "rawQuery": query,
            "count": 20,
            "product": "Latest",
            "querySource": "typed_query",
        }
        if cursor:
            variables["cursor"] = cursor
        params = encode_params(
            {
                "variables": variables,
                "features": GQL_FEATURES,
                "fieldToggles": {"withArticleRichContentState": True},
            }
        )
        # Bypass upstream retry/end wrappers so every returned body reaches our ledger.
        from twscrape.http import ConnectError, NetworkError

        try:
            response = await ctx.clt.request(
                "GET",
                url,
                params=params,
                headers={"x-client-transaction-id": generator.calc("GET", urlsplit(url).path)},
            )
        except (ConnectError, NetworkError) as exc:
            raise Transient("network_error", type(exc).__name__) from None
        ctx.req_count += 1
        try:
            actual = json.loads(parse_qs(urlsplit(str(response.url)).query)["variables"][0])
        except (ValueError, KeyError, IndexError):
            actual = {}  # Body still archived; validator protects request mismatch.
        try:
            body = response.json()
        except ValueError:
            body = {"non_json_body": response.text[:65536]}
        rows, errors = [], 0
        if response.status_code == 200 and isinstance(body, dict) and not body.get("errors"):
            try:
                rows, errors = strict_rows(body)
            except Exception:
                errors = 1
        if not isinstance(body, dict):
            body = {"non_object_body": body}
            errors += 1
        return Page(
            response.status_code,
            body,
            dict(response.headers),
            actual.get("rawQuery", ""),
            actual.get("cursor", ""),
            rows,
            errors,
        )
