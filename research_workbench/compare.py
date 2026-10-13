"""Compare two fresh, identically configured pilot states, not archived totals."""

import argparse
import json
from pathlib import Path


def compare(left, right):
    fields = [
        "query",
        "day",
        "sort",
        "timezone",
        "account",
        "delay_seconds",
        "retry_limit",
        "backoff_seconds",
        "http_backend",
    ]
    differences = [key for key in fields if left["config"].get(key) != right["config"].get(key)]
    fresh = all(
        len(state["invocations"]) == 1 and not state["invocations"][0]["resume"]
        for state in (left, right)
    )
    same_budget = left["invocations"][0]["page_budget"] == right["invocations"][0]["page_budget"]
    comparable = (
        fresh
        and same_budget
        and not differences
        and not any(x["guard_hold"] for x in (left, right))
    )
    ids = [{row["post_id"] for row in state["posts"]} for state in (left, right)]

    def metrics(state):
        seconds = state["active_seconds"]
        return {
            "state": state["state"],
            "search_end_verified": state["search_end_verified"],
            "actual_saved": len(state["posts"]),
            "parsed": state["parsed_count"],
            "date_rejected": state["date_rejected_count"],
            "pages": len(state["pages"]),
            "measured_seconds": seconds,
            "saved_posts_per_minute": (
                len(state["posts"]) * 60 / seconds if seconds and comparable else None
            ),
        }

    union = ids[0] | ids[1]
    return {
        "config_differences": differences,
        "fresh_runs": fresh,
        "same_page_budget": same_budget,
        "speed_comparison_eligible": comparable,
        "left": metrics(left),
        "right": metrics(right),
        "common_ids": len(ids[0] & ids[1]),
        "left_only": len(ids[0] - ids[1]),
        "right_only": len(ids[1] - ids[0]),
        "id_jaccard": (len(ids[0] & ids[1]) / len(union) if union else None),
        "platform_all_posts_proven": False,
        "caveat": "Same alias is not proof of same real account. Verify account/query/time conditions manually. "
        "Elapsed includes signing/page pacing/cooldowns/checkpoint writes; excludes initial account client setup and final export. "
        "One paired run cannot establish general speed superiority or platform completeness.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args()
    states = [json.loads(path.read_text(encoding="utf-8")) for path in (args.left, args.right)]
    print(json.dumps(compare(*states), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
