import copy

import pytest

from research_workbench.compare import compare


def state():
    return {
        "config": {
            "query": "gpt exact-day",
            "day": "2026-06-25",
            "sort": "Latest",
            "timezone": "+08:00",
            "account": "test",
            "delay_seconds": 4,
            "retry_limit": 3,
            "backoff_seconds": 180,
        },
        "invocations": [{"resume": False, "page_budget": 5}],
        "guard_hold": None,
        "state": "page_budget_reached",
        "search_end_verified": False,
        "posts": [{"post_id": "1"}],
        "active_seconds": 60,
        "parsed_count": 1,
        "date_rejected_count": 0,
        "pages": [{}],
    }


@pytest.mark.parametrize("fault", ["query", "account", "resume", "budget", "hold"])
def test_unmatched_or_resumed_runs_do_not_produce_speed_claim(fault):
    left, right = state(), state()
    if fault in {"query", "account"}:
        right["config"][fault] = "different"
    elif fault == "resume":
        right["invocations"].append({"resume": True, "page_budget": 5})
    elif fault == "budget":
        right["invocations"][0]["page_budget"] = 10
    elif fault == "hold":
        right["guard_hold"] = {"code": "rate_limited"}
    result = compare(left, right)
    assert not result["speed_comparison_eligible"]
    assert result["right"]["saved_posts_per_minute"] is None


def test_matched_fresh_runs_report_id_difference_not_fullness():
    left = state()
    right = copy.deepcopy(left)
    right["posts"].append({"post_id": "2"})
    result = compare(left, right)
    assert result["speed_comparison_eligible"] and result["common_ids"] == 1
    assert result["right_only"] == 1 and result["id_jaccard"] == 0.5
    assert not result["platform_all_posts_proven"]
