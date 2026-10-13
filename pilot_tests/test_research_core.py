import asyncio
import copy
import json

import pytest

from research_workbench.core import Config, Hold, Page, collect, verify_evidence


def body(cursor=None, kind="tweets"):
    if kind == "tail":
        instructions = [
            {
                "type": "TimelineReplaceEntry",
                "entry": {"content": {"cursorType": "Bottom", "value": cursor}},
            }
        ]
    elif kind == "end":
        instructions = [{"type": "TimelineTerminateTimeline", "direction": "Bottom"}]
    else:
        entries = [{"entryId": "tweet-fixture", "content": {"fixture": True}}]
        if cursor:
            entries.append(
                {"entryId": "cursor-bottom", "content": {"cursorType": "Bottom", "value": cursor}}
            )
        instructions = [{"type": "TimelineAddEntries", "entries": entries}]
    return {
        "data": {
            "search_by_raw_query": {"search_timeline": {"timeline": {"instructions": instructions}}}
        }
    }


def row(identifier="1", day="2026-06-25", text="gpt example\nsecond line"):
    return {
        "post_id": identifier,
        "posted_at": day + "T12:00:00+08:00",
        "post_text": text,
        "post_url": "https://x.com/example/status/" + identifier,
        "author_username": "example",
        "profile_location": "",
    }


CFG = Config("gpt", "2026-06-25", "independent-test")


def page(cursor, next_cursor=None, rows=None, kind="tweets", status=200, **extra):
    return Page(
        status,
        body(next_cursor, kind),
        {},
        CFG.query,
        cursor,
        rows if rows is not None else [row()],
        **extra,
    )


class FakeTransport:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    async def fetch(self, query, cursor):
        self.requests.append((query, cursor))
        item = next(self.pages)
        if isinstance(item, Exception):
            raise item
        return item


async def no_sleep(seconds):
    pass


@pytest.mark.asyncio
async def test_tail_requires_three_raw_pages_and_preserves_tsv(tmp_path):
    transport = FakeTransport(
        [
            page("", "c1"),
            page("c1", "c2", [], "tail"),
            page("c2", "c3", [], "tail"),
            page("c3", "c4", [], "tail"),
        ]
    )
    state = await collect(CFG, tmp_path, transport, sleep=no_sleep)
    assert state["state"] == "validated_empty_tail" and state["search_end_verified"]
    assert not state["platform_all_posts_proven"] and len(state["posts"]) == 1
    assert [x[1] for x in transport.requests] == ["", "c1", "c2", "c3"]
    verify_evidence(tmp_path, state)
    assert len((tmp_path / "posts.txt").read_text(encoding="utf-8-sig").splitlines()) == 2
    assert "\\nsecond line" in (tmp_path / "posts.txt").read_text(encoding="utf-8-sig")
    assert len(list((tmp_path / "responses").glob("*.json"))) == 4


@pytest.mark.asyncio
async def test_page_budget_resume_starts_exact_cursor_and_deduplicates(tmp_path):
    first = await collect(
        CFG, tmp_path, FakeTransport([page("", "c1")]), max_pages=1, sleep=no_sleep
    )
    assert first["state"] == "page_budget_reached" and not first["search_end_verified"]
    transport = FakeTransport([page("c1", None, [row(), row("2")])])
    state = await collect(CFG, tmp_path, transport, resume=True, sleep=no_sleep)
    assert transport.requests == [(CFG.query, "c1")]
    assert [x["post_id"] for x in state["posts"]] == ["1", "2"]
    assert state["state"] == "response_end" and state["search_end_verified"]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
@pytest.mark.asyncio
async def test_auth_endpoint_errors_saved_without_retry_or_end(tmp_path, status):
    transport = FakeTransport([page("", status=status)])
    state = await collect(CFG, tmp_path, transport, sleep=no_sleep)
    assert state["guard_hold"] and not state["search_end_verified"]
    assert len(transport.requests) == 1 and len(state["attempts"]) == 1
    assert len(state["pages"]) == 0


@pytest.mark.parametrize(
    "fault,code",
    [
        ("date", "date_filter_mismatch"),
        ("cursor", "query_cursor_mismatch"),
        ("query", "query_cursor_mismatch"),
        ("parser", "parse_failure"),
        ("business", "api_error"),
        ("unclassified_empty", "unclassified_empty"),
        ("empty_instructions", "invalid_response"),
        ("unknown_instructions", "invalid_response"),
    ],
)
@pytest.mark.asyncio
async def test_quality_failures_never_look_like_end(tmp_path, fault, code):
    candidate = page("", None)
    if fault == "date":
        candidate.rows = [row(day="2026-06-26")]
    if fault == "cursor":
        candidate.request_cursor = "wrong"
    if fault == "query":
        candidate.query = "wrong"
    if fault == "parser":
        candidate.parse_errors = 1
    if fault == "business":
        candidate.body["errors"] = [{"code": 336}]
    if fault == "unclassified_empty":
        candidate.rows = []
    if fault == "empty_instructions":
        candidate.body["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
            "instructions"
        ] = []
    if fault == "unknown_instructions":
        candidate.body["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
            "instructions"
        ] = [{"type": "NewUnknownType"}]
    state = await collect(CFG, tmp_path, FakeTransport([candidate]), sleep=no_sleep)
    assert state["state"] == code and state["guard_hold"] and not state["search_end_verified"]
    assert not state["posts"]


@pytest.mark.asyncio
async def test_normal_empty_requires_explicit_end(tmp_path):
    state = await collect(CFG, tmp_path, FakeTransport([page("", None, [], "end")]), sleep=no_sleep)
    assert state["search_end_verified"] and state["state"] == "response_end" and not state["posts"]


@pytest.mark.asyncio
async def test_repeated_cursor_preserves_sample_but_not_end(tmp_path):
    state = await collect(
        CFG, tmp_path, FakeTransport([page("", "c1"), page("c1", "c1", [row("2")])]), sleep=no_sleep
    )
    assert len(state["posts"]) == 2 and state["state"] == "repeated_cursor"
    assert not state["search_end_verified"]


@pytest.mark.asyncio
async def test_reset_not_shortened_and_429_response_archived(tmp_path):
    import time

    limited = page("", status=429)
    limited.headers = {"x-rate-limit-reset": str(time.time() + 900), "Cookie": "NEVER_STORE"}
    delays = []

    async def sleep(seconds):
        delays.append(seconds)

    state = await collect(CFG, tmp_path, FakeTransport([limited, page("", None)]), sleep=sleep)
    assert delays[0] >= 900 and len(state["attempts"]) == 2
    assert state["search_end_verified"]
    assert "Cookie" not in state["attempts"][0]["headers"]


@pytest.mark.asyncio
async def test_existing_guard_missing_checkpoint_and_drift_refuse_before_requests(tmp_path):
    transport = FakeTransport([])
    with pytest.raises(ValueError):
        await collect(CFG, tmp_path, transport, resume=True)
    await collect(CFG, tmp_path, FakeTransport([page("", status=404)]), sleep=no_sleep)
    with pytest.raises(ValueError):
        await collect(CFG, tmp_path, transport, resume=True)
    with pytest.raises(ValueError):
        await collect(Config("kimi", CFG.day, CFG.account), tmp_path, transport, resume=True)
    assert not transport.requests


@pytest.mark.asyncio
async def test_tampered_raw_or_end_label_cannot_be_reused(tmp_path):
    state = await collect(
        CFG, tmp_path, FakeTransport([page("", "c1")]), max_pages=1, sleep=no_sleep
    )
    fabricated = copy.deepcopy(state)
    fabricated.update(state="validated_empty_tail", search_end_verified=True)
    with pytest.raises(Hold):
        verify_evidence(tmp_path, fabricated)
    (tmp_path / state["pages"][0]["file"]).write_text("{}")
    with pytest.raises(Hold):
        await collect(CFG, tmp_path, FakeTransport([]), resume=True)


@pytest.mark.asyncio
async def test_cancel_saves_user_pause_and_retains_cursor(tmp_path):
    transport = FakeTransport([page("", "c1"), asyncio.CancelledError()])

    async def cancel(_):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await collect(CFG, tmp_path, transport, sleep=cancel)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert (
        state["state"] == "user_paused"
        and state["next_cursor"] == "c1"
        and len(state["posts"]) == 1
    )


@pytest.mark.parametrize("kw", [{"delay_seconds": 1}, {"retry_limit": 4}, {"backoff_seconds": 1}])
def test_no_unsafe_acceleration(kw):
    with pytest.raises(ValueError):
        Config("gpt", "2026-06-25", "test", **kw)


def test_beijing_half_open_query():
    assert CFG.query == "gpt since_time:1782316800 until_time:1782403200"


@pytest.mark.asyncio
async def test_storage_error_never_retried_as_network(tmp_path):
    transport = FakeTransport([page("", "c1"), OSError("fixture storage failure")])
    with pytest.raises(OSError):
        await collect(CFG, tmp_path, transport, sleep=no_sleep)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "storage_failure" and len(state["posts"]) == 1
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_unexpected_failure_is_protected_not_left_in_progress(tmp_path):
    with pytest.raises(RuntimeError):
        await collect(CFG, tmp_path, FakeTransport([RuntimeError("fixture")]), sleep=no_sleep)
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "unexpected_error" and state["guard_hold"]


@pytest.mark.asyncio
async def test_http_date_retry_after_is_honored(tmp_path):
    import email.utils
    import time
    from datetime import datetime, timezone

    limited = page("", status=429)
    limited.headers = {
        "retry-after": email.utils.format_datetime(
            datetime.fromtimestamp(time.time() + 1000, timezone.utc), usegmt=True
        )
    }
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    await collect(CFG, tmp_path, FakeTransport([limited, page("", None)]), sleep=sleep)
    assert waits[0] > 990


@pytest.mark.asyncio
async def test_budget_return_preserves_reset_and_resume_waits_before_request(tmp_path):
    import time

    first = page("", "c1")
    first.headers = {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(time.time() + 1000)}
    state = await collect(CFG, tmp_path, FakeTransport([first]), max_pages=1, sleep=no_sleep)
    assert state["state"] == "page_budget_reached" and state["cooldown_until"] > time.time() + 990
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    await collect(CFG, tmp_path, FakeTransport([page("c1", None)]), resume=True, sleep=sleep)
    assert waits[0] > 990
