"""Auditable single-day collection; pure stdlib policy and durable checkpoints."""

import asyncio
import csv
import email.utils
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

POLICY = "research_pilot_v1"
TZ = timezone(timedelta(hours=8))
RATE_HEADERS = {"retry-after", "x-rate-limit-limit", "x-rate-limit-remaining", "x-rate-limit-reset"}


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@dataclass(frozen=True)
class Config:
    keyword: str
    day: str
    account: str
    delay_seconds: float = 4.0
    retry_limit: int = 3
    backoff_seconds: float = 180.0

    def __post_init__(self):
        date.fromisoformat(self.day)
        if not self.keyword.strip() or not self.account or self.delay_seconds < 2:
            raise ValueError("Nonempty keyword/account and delay >=2 seconds required")
        if not 0 <= self.retry_limit <= 3 or self.backoff_seconds < 180:
            raise ValueError("Retry budget <=3 and backoff >=180 seconds required")

    @property
    def bounds(self):
        start = datetime.combine(date.fromisoformat(self.day), datetime.min.time(), TZ)
        return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())

    @property
    def query(self):
        start, end = self.bounds
        return f"{self.keyword} since_time:{start} until_time:{end}"

    def receipt(self):
        return {
            **asdict(self),
            "query": self.query,
            "sort": "Latest",
            "timezone": "+08:00",
            "policy": POLICY,
            "http_backend": "httpx",
            "upstream_base_commit": "568844df82af956df5970bcf7ff84dd5ba729eb0",
            "about_requests": False,
            "account_rotation": False,
        }


@dataclass
class Page:
    status: int
    body: dict
    headers: dict
    query: str
    request_cursor: str
    rows: list
    parse_errors: int = 0


class Hold(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Transient(Hold):
    def __init__(self, code, message, wait_until=0):
        super().__init__(code, message)
        self.wait_until = wait_until


def number(value, default=0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def retry_deadline(value):
    try:
        return time.time() + max(0, float(value))
    except (ValueError, TypeError):
        try:
            stamp = email.utils.parsedate_to_datetime(value)
            return stamp.timestamp() if stamp.tzinfo else 0
        except (ValueError, TypeError, IndexError):
            return 0


def timeline(body):
    try:
        instructions = body["data"]["search_by_raw_query"]["search_timeline"]["timeline"][
            "instructions"
        ]
    except (KeyError, TypeError):
        raise Hold("invalid_response", "SearchTimeline instructions absent") from None
    known = {
        "TimelineAddEntries",
        "TimelineReplaceEntry",
        "TimelineTerminateTimeline",
        "TimelineClearCache",
        "TimelinePinEntry",
    }
    if (
        not isinstance(instructions, list)
        or not instructions
        or any(not isinstance(x, dict) or x.get("type") not in known for x in instructions)
    ):
        raise Hold("invalid_response", "Unknown or empty timeline instructions")
    return instructions


def bottom_cursor(body):
    values = []

    def visit(obj):
        if isinstance(obj, dict):
            if obj.get("cursorType") == "Bottom":
                if not isinstance(obj.get("value"), str) or not obj["value"]:
                    raise Hold("invalid_response", "Invalid Bottom cursor")
                values.append(obj["value"])
            for val in obj.values():
                visit(val)
        elif isinstance(obj, list):
            for val in obj:
                visit(val)

    visit(body)
    if len(set(values)) > 1:
        raise Hold("invalid_response", "Ambiguous Bottom cursors")
    return values[0] if values else ""


def cursor_only(body):
    for item in timeline(body):
        content = (item.get("entry") or {}).get("content") or {}
        if (
            item["type"] != "TimelineReplaceEntry"
            or content.get("cursorType") not in {"Top", "Bottom"}
            or not content.get("value")
        ):
            return False
    return bool(bottom_cursor(body))


def explicit_empty_end(body):
    # Do not treat tombstones, modules or failed parsing as a normal empty day.
    return all(
        x["type"] == "TimelineTerminateTimeline"
        or (x["type"] == "TimelineAddEntries" and x.get("entries") == [])
        for x in timeline(body)
    )


def validate_page(page, config, cursor):
    if page.query != config.query or page.request_cursor != cursor:
        raise Hold("query_cursor_mismatch", "Actual request differs from checkpoint")
    if page.status in {400, 401, 403, 404}:
        raise Hold(
            "authentication_hold" if page.status in {401, 403} else "endpoint_hold",
            f"HTTP {page.status}; do not mark ended or rotate accounts",
        )
    reset = number(page.headers.get("x-rate-limit-reset"))
    retry_after = retry_deadline(page.headers.get("retry-after"))
    if page.status == 429:
        raise Transient("rate_limited", "HTTP 429", max(reset, retry_after))
    if page.status >= 500:
        raise Transient("server_error", f"HTTP {page.status}")
    if page.status != 200:
        raise Hold("http_error", f"HTTP {page.status}")
    if page.body.get("errors"):
        codes = {x.get("code") for x in page.body["errors"] if isinstance(x, dict)}
        if codes & {32, 89, 326}:
            raise Hold("authentication_hold", "API authentication error")
        if 88 in codes:
            raise Transient("rate_limited", "API rate limit", max(reset, retry_after))
        raise Hold("api_error", "HTTP200 contains business errors; not an empty response")
    timeline(page.body)
    if page.parse_errors:
        raise Hold("parse_failure", "Some tweet objects could not be parsed")
    if not page.rows and not cursor_only(page.body) and not explicit_empty_end(page.body):
        raise Hold("unclassified_empty", "Empty parsed output is not a canonical empty response")
    start, end = config.bounds
    for row in page.rows:
        try:
            stamp = datetime.fromisoformat(row["posted_at"].replace("Z", "+00:00"))
            valid = stamp.tzinfo is not None and start <= stamp.timestamp() < end
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise Hold("date_filter_mismatch", "Returned tweet outside target natural day")
        if not row.get("post_id"):
            raise Hold("parse_failure", "Tweet without stable ID")
    return bottom_cursor(page.body)


def verify_evidence(folder, state):
    cursor = ""
    for entry in state["pages"]:
        if (
            entry["http_status"] != 200
            or entry["query"] != state["config"]["query"]
            or entry["request_cursor"] != cursor
        ):
            raise Hold("invalid_end_evidence", "Broken durable query/cursor chain")
        blob = (folder / entry["file"]).read_bytes()
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise Hold("invalid_end_evidence", "Raw response hash mismatch")
        body = json.loads(blob)
        timeline(body)
        if body.get("errors"):
            raise Hold("invalid_end_evidence", "Business error in accepted raw response")
        if bottom_cursor(body) != entry["next_cursor"]:
            raise Hold("invalid_end_evidence", "Raw response cursor differs from ledger")
        cursor = entry["next_cursor"]
    if cursor != state["next_cursor"]:
        raise Hold("invalid_end_evidence", "Checkpoint cursor differs from ledger")
    if state["search_end_verified"]:
        if state.get("guard_hold") or state["date_rejected_count"] or not state["pages"]:
            raise Hold(
                "invalid_end_evidence", "Protected/rejected/missing evidence cannot be ended"
            )
        if state["state"] == "response_end":
            if cursor:
                raise Hold("invalid_end_evidence", "Claimed response end still has Bottom cursor")
        elif state["state"] == "validated_empty_tail":
            tail = state["pages"][-3:]
            if (
                len(tail) < 3
                or not state["posts"]
                or any(
                    x["parsed_count"]
                    or not cursor_only(json.loads((folder / x["file"]).read_bytes()))
                    for x in tail
                )
            ):
                raise Hold(
                    "invalid_end_evidence", "Claimed empty tail lacks three canonical responses"
                )
        else:
            raise Hold("invalid_end_evidence", "A completion label is not end evidence")


def export_txt(folder, state):
    def escape(value):
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\t", "\\t")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
        )

    path = folder / "posts.txt"
    temporary = path.with_suffix(".txt.tmp")
    columns = [
        "keyword",
        "posted_at",
        "post_id",
        "post_text",
        "post_url",
        "author_username",
        "profile_location",
        "body_keyword_hit",
    ]
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        stream.write("\t".join(columns) + "\n")
        for row in state["posts"]:
            stream.write("\t".join(escape(row.get(key, "")) for key in columns) + "\n")
    os.replace(temporary, path)
    with (folder / "daily_ledger.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        fields = [
            "day",
            "keyword",
            "pages",
            "parsed",
            "date_rejected",
            "saved",
            "body_hit",
            "state",
            "search_end_verified",
            "platform_all_posts_proven",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "day": state["config"]["day"],
                "keyword": state["config"]["keyword"],
                "pages": len(state["pages"]),
                "parsed": state["parsed_count"],
                "date_rejected": state["date_rejected_count"],
                "saved": len(state["posts"]),
                "body_hit": sum(p["body_keyword_hit"] for p in state["posts"]),
                "state": state["state"],
                "search_end_verified": state["search_end_verified"],
                "platform_all_posts_proven": False,
            }
        )


async def collect(config, folder, transport, *, resume=False, max_pages=5, sleep=asyncio.sleep):
    if not 1 <= max_pages <= 1000:
        raise ValueError("Per-invocation page budget must be 1..1000")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "state.json"
    if path.exists():
        if not resume:
            raise ValueError("Existing run; use --resume, never overwrite")
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["config"] != config.receipt():
            raise ValueError("Configuration drift; do not resume")
        verify_evidence(folder, state)
        if state.get("guard_hold"):
            raise ValueError("Existing protection; do not automatically clear")
        if state["search_end_verified"]:
            return state
    else:
        if resume:
            raise ValueError("Resume checkpoint does not exist; no page1 fallback")
        state = {
            "config": config.receipt(),
            "created_at": now(),
            "posts": [],
            "pages": [],
            "attempts": [],
            "next_cursor": "",
            "parsed_count": 0,
            "date_rejected_count": 0,
            "empty_tail": [],
            "state": "in_progress",
            "search_end_verified": False,
            "platform_all_posts_proven": False,
            "guard_hold": None,
            "active_seconds": 0.0,
            "invocations": [],
        }
    started = time.monotonic()
    state["invocations"].append({"started_at": now(), "page_budget": max_pages, "resume": resume})
    known = {p["post_id"] for p in state["posts"]}

    def save():
        state["updated_at"] = now()
        atomic_json(path, state)
        atomic_json(
            folder / "status.json",
            {
                k: v
                for k, v in state.items()
                if k not in {"posts", "pages", "attempts", "empty_tail"}
            }
            | {
                "saved_posts": len(state["posts"]),
                "saved_pages": len(state["pages"]),
                "worker_pid": os.getpid(),
            },
        )

    save()
    try:
        # Honor persisted server cooling across restarts, not just within a process.
        until = state.get("cooldown_until", 0)
        if until > time.time():
            state["state"] = state.get("cooldown_reason", "cooldown")
            save()
            await sleep(until - time.time())
        for _ in range(max_pages):
            cursor = state["next_cursor"]
            for attempt in range(config.retry_limit + 1):
                state["state"] = "in_progress"
                save()
                try:
                    page = await transport.fetch(config.query, cursor)
                except Transient as exc:
                    error = exc
                    page = None
                except asyncio.TimeoutError as exc:
                    error = Transient("network_error", type(exc).__name__)
                    page = None
                if page is not None:
                    # Capture the body before parsing/classifying/advancing the cursor.
                    index = len(state["attempts"]) + 1
                    relative = f"responses/attempt_{index:05d}.json"
                    atomic_json(folder / relative, page.body)
                    evidence = {
                        "file": relative,
                        "sha256": hashlib.sha256((folder / relative).read_bytes()).hexdigest(),
                        "query": page.query,
                        "request_cursor": page.request_cursor,
                        "http_status": page.status,
                        "headers": {
                            k.lower(): v
                            for k, v in page.headers.items()
                            if k.lower() in RATE_HEADERS
                        },
                        "received_at": now(),
                        "parsed_count": len(page.rows),
                        "parse_errors": page.parse_errors,
                    }
                    state["attempts"].append(evidence)
                    state["parsed_count"] += len(page.rows)
                    try:
                        next_cursor = validate_page(page, config, cursor)
                        break
                    except Transient as exc:
                        error = exc
                    except Hold as exc:
                        if exc.code == "date_filter_mismatch":
                            start, end = config.bounds
                            for row in page.rows:
                                try:
                                    stamp = datetime.fromisoformat(
                                        row["posted_at"].replace("Z", "+00:00")
                                    )
                                    state["date_rejected_count"] += not (
                                        stamp.tzinfo and start <= stamp.timestamp() < end
                                    )
                                except (KeyError, ValueError, TypeError):
                                    state["date_rejected_count"] += 1
                        save()
                        raise
                if attempt >= config.retry_limit:
                    raise Hold(error.code, "Original bounded retry budget exhausted")
                wait = max(
                    config.backoff_seconds * (attempt + 1), error.wait_until - time.time() + 10
                )
                state.update(
                    state=error.code, cooldown_reason=error.code, cooldown_until=time.time() + wait
                )
                save()
                await sleep(wait)
            new_rows = []
            for row in page.rows:
                row = {
                    **row,
                    "keyword": config.keyword,
                    "body_keyword_hit": config.keyword.casefold()
                    in row.get("post_text", "").casefold(),
                }
                if row["post_id"] not in known:
                    known.add(row["post_id"])
                    new_rows.append(row)
            state["posts"].extend(new_rows)
            entry = {
                **evidence,
                "page": len(state["pages"]) + 1,
                "next_cursor": next_cursor,
                "new_saved_count": len(new_rows),
            }
            state["pages"].append(entry)
            state["next_cursor"] = next_cursor
            state["cooldown_until"] = 0
            if not next_cursor:
                state["state"] = "response_end"
                state["search_end_verified"] = True
            elif next_cursor == cursor or next_cursor in {
                p["request_cursor"] for p in state["pages"]
            }:
                raise Hold("repeated_cursor", "Repeated cursor; not search ended")
            elif not page.rows and cursor_only(page.body):
                state["empty_tail"].append(entry)
                if len(state["empty_tail"]) >= 3 and state["posts"]:
                    state["state"] = "validated_empty_tail"
                    state["search_end_verified"] = True
            else:
                state["empty_tail"] = []
            save()
            if state["search_end_verified"]:
                verify_evidence(folder, state)
                break
            reset = number(page.headers.get("x-rate-limit-reset"))
            pause = config.delay_seconds
            if number(page.headers.get("x-rate-limit-remaining"), 999) <= 5 and reset > time.time():
                pause = max(pause, reset - time.time() + 10)
                state.update(state="rate_limited", cooldown_reason="rate_limited")
            else:
                state["cooldown_reason"] = "cooldown"
            state["cooldown_until"] = time.time() + pause
            save()
            if _ == max_pages - 1:
                state["state"] = "page_budget_reached"
                break
            await sleep(pause)
        else:
            state["state"] = "page_budget_reached"
    except Hold as exc:
        state.update(
            state=exc.code,
            search_end_verified=False,
            guard_hold={"at": now(), "code": exc.code, "message": str(exc)},
        )
    except asyncio.CancelledError:
        state["state"] = "user_paused"
        raise
    except OSError:
        # Let actual storage failures remain fatal, not network/empty-day results.
        state.update(
            state="storage_failure",
            search_end_verified=False,
            guard_hold={"at": now(), "code": "storage_failure"},
        )
        raise
    except Exception as exc:
        state.update(
            state="unexpected_error",
            search_end_verified=False,
            guard_hold={"at": now(), "code": "unexpected_error", "type": type(exc).__name__},
        )
        raise
    finally:
        state["active_seconds"] += time.monotonic() - started
        save()
        export_txt(folder, state)
    return state
