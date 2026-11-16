"""SPEC 6.1: the pattern digest's statistics, on hand-built data and on the seed."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from task_orchard.core.clock import FixedClock
from task_orchard.core.enums import FieldSource, Level, Status
from task_orchard.core.service import Orchard
from task_orchard.db.models import Completion, Task
from tests.helpers import make
from tests.test_roll_forward import live, weekly


def pattern(orchard: Orchard, name: str = "Reports") -> int:
    return orchard.save_pattern(name=name)["pattern"]["id"]


def done(
    orchard: Orchard,
    pattern_id: int,
    days_ago: float,
    *,
    actual: int | None = None,
    estimate: int | None = None,
    source: str | None = "guess",
    activation: int | None = None,
    drain: int | None = None,
    status: Status = Status.DONE,
    **task: Any,
) -> int:
    """A closed member written directly, so tests can build many quickly."""
    with orchard.tx() as tx:
        when = tx.now - timedelta(days=days_ago)
        row = Task(
            title="Member",
            status=status,
            created_at=when,
            updated_at=when,
            closed_at=when,
            pattern_id=pattern_id,
            triaged=True,
            **task,
        )
        tx.session.add(row)
        tx.session.flush()
        if status is Status.DONE:
            tx.session.add(
                Completion(
                    task_id=row.id,
                    completed_at=when,
                    actual_minutes=actual,
                    estimate_at_completion=estimate,
                    estimate_source_at_completion=(
                        None if estimate is None or source is None else FieldSource(source)
                    ),
                    felt_activation=None if activation is None else Level(activation),
                    felt_drain=None if drain is None else Level(drain),
                )
            )
        return row.id


def digest(orchard: Orchard, pattern_id: int) -> dict[str, Any]:
    return orchard.save_pattern(pattern_id)["pattern"]


# --- Hand-built ---


def test_medians_on_even_counts(orchard: Orchard) -> None:
    p = pattern(orchard)
    done(orchard, p, 4, actual=10, estimate=10, activation=1, drain=2)
    done(orchard, p, 3, actual=25, estimate=10, activation=2, drain=3)
    d = digest(orchard, p)
    assert d["actual_minutes"] == 17.5
    assert d["ratio"] == 1.8  # median of 1.0 and 2.5 is 1.75
    assert d["ratio_samples"] == 2
    assert d["felt_activation"] == "medium"  # 1.5 rounds up
    assert d["felt_drain"] == "high"  # 2.5 rounds up, not to even


def test_odd_counts_and_whole_numbers(orchard: Orchard) -> None:
    p = pattern(orchard)
    for minutes in (30, 10, 20):
        done(orchard, p, 1, actual=minutes, estimate=20, drain=1)
    d = digest(orchard, p)
    assert d["actual_minutes"] == 20
    assert isinstance(d["actual_minutes"], int)
    assert d["ratio"] == 1.0
    assert d["felt_drain"] == "low"
    assert "felt_activation" not in d


def test_the_window_is_the_20_most_recent_that_have_the_value(orchard: Orchard) -> None:
    p = pattern(orchard)
    for i in range(20):  # recent: 10 minutes each
        done(orchard, p, i + 1, actual=10, estimate=10, activation=1)
    for i in range(30):  # older: 100 minutes each, outside the window
        done(orchard, p, 30 + i, actual=100, estimate=10, activation=3)
    for i in range(5):  # most recent of all, but with no values: skipped, not counted
        done(orchard, p, 0.1 * (i + 1))
    d = digest(orchard, p)
    assert d["actual_minutes"] == 10
    assert d["ratio"] == 1.0 and d["ratio_samples"] == 20
    assert d["felt_activation"] == "low"
    assert d["done"] == 55


def test_pattern_and_evidence_estimates_are_left_out_of_the_ratio(orchard: Orchard) -> None:
    p = pattern(orchard)
    done(orchard, p, 5, actual=60, estimate=20, source="user")
    done(orchard, p, 4, actual=60, estimate=20, source="guess")
    done(orchard, p, 3, actual=60, estimate=60, source="pattern")
    done(orchard, p, 2, actual=60, estimate=60, source="evidence")
    done(orchard, p, 1, actual=60, estimate=None)
    d = digest(orchard, p)
    assert d["ratio"] == 3.0
    assert d["ratio_samples"] == 2
    assert d["actual_minutes"] == 60  # every completion with an actual counts here


def test_recurring_containers_are_not_members(orchard: Orchard) -> None:
    p = pattern(orchard)
    laundry = orchard.create_task(
        "Laundry", recurrence={"mode": "flexible"}, pattern_id=p, estimate_minutes=40
    )["task"]["id"]
    orchard.complete_task(laundry, pattern_id=p, actual_minutes=30)  # the instance is filed
    d = digest(orchard, p)
    assert d["done"] == 1
    assert "open" not in d  # the open recurring task is not counted
    assert d["actual_minutes"] == 30


def test_counts_deferrals_and_false_starts(orchard: Orchard) -> None:
    p = pattern(orchard)
    restarted = make(orchard, pattern_id=p)
    for _ in range(3):
        orchard.start_task(restarted)
    orchard.snooze_task(restarted, days=1)
    orchard.snooze_task(restarted, days=0)
    orchard.complete_task(restarted)  # 3 starts, done: 2 false starts
    abandoned = make(orchard, pattern_id=p)
    orchard.start_task(abandoned)
    orchard.snooze_task(abandoned, days=2)
    orchard.drop_tasks([abandoned], reason="x")  # 1 start, dropped: 1 false start
    still_open = make(orchard, pattern_id=p)
    orchard.start_task(still_open)
    orchard.start_task(still_open)  # open: not counted
    d = digest(orchard, p)
    assert d["false_starts"] == 3
    assert d["deferred"] == 2
    assert (d["done"], d["dropped"], d["open"]) == (1, 1, 1)


def test_last_done_and_gaps(orchard: Orchard) -> None:
    p = pattern(orchard)
    for days in (30, 20, 14, 10, 7, 5, 4.5):
        done(orchard, p, days)
    d = digest(orchard, p)
    assert d["last_done"]["rel"] == "4 days ago"
    assert d["gaps_days"] == [0.5, 2.0, 3.0, 4.0, 6.0]  # newest first, the last 5


def test_one_completion_has_no_gaps_and_none_has_no_statistics(orchard: Orchard) -> None:
    p = pattern(orchard)
    assert set(digest(orchard, p)) == {"id", "name"}
    done(orchard, p, 2)
    d = digest(orchard, p)
    assert "gaps_days" not in d and "last_done" in d


def test_missed_instances_are_members(orchard: Orchard, clock: FixedClock) -> None:
    p = pattern(orchard)
    summary = weekly(orchard)
    clock.advance(days=1)
    orchard.get_critical()
    [first] = live(orchard, summary)
    orchard.update_task(first.id, pattern_id=p)
    clock.advance(weeks=2)  # two more Fridays: the cap closes the first as missed
    assert digest(orchard, p)["missed"] == 1


def test_the_digest_is_a_fixed_set_of_keys(orchard: Orchard, seeded: dict[str, int]) -> None:
    allowed = {
        "id", "name", "description", "learned", "done", "dropped", "missed", "open",
        "deferred", "false_starts", "actual_minutes", "ratio", "ratio_samples",
        "felt_activation", "felt_drain", "last_done", "gaps_days",
    }  # fmt: skip
    for row in orchard.find_patterns(limit=10)["patterns"]:
        assert set(row) <= allowed, row


# --- The seed ---


def test_interface_specs_run_long(orchard: Orchard, seeded: dict[str, int]) -> None:
    [specs] = orchard.find_patterns("Interface specs")["patterns"]
    assert specs["ratio"] > 2
