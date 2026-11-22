"""SPEC 6.3: the want / should / must balance."""

from __future__ import annotations

from datetime import timedelta

from task_orchard.core import history
from task_orchard.core.enums import FieldSource, Motive, Status
from task_orchard.core.service import Orchard
from task_orchard.db.models import Completion, Task

ZERO = {"want": 0, "should": 0, "must": 0, "unset": 0}


def done(
    orchard: Orchard,
    days_ago: float,
    motive: str | None,
    actual: int | None = None,
    estimate: int | None = None,
) -> None:
    with orchard.tx() as tx:
        when = tx.now - timedelta(days=days_ago)
        task = Task(title="T", status=Status.DONE, created_at=when, updated_at=when, closed_at=when)
        tx.session.add(task)
        tx.session.flush()
        tx.session.add(
            Completion(
                task_id=task.id,
                completed_at=when,
                actual_minutes=actual,
                estimate_at_completion=estimate,
                estimate_source_at_completion=None if estimate is None else FieldSource.GUESS,
                motive_at_completion=None if motive is None else Motive(motive),
            )
        )


def balance(orchard: Orchard) -> dict:
    with orchard.tx() as tx:
        return history.balance(tx.session, tx.now)


def test_an_empty_history_keeps_every_zero(orchard: Orchard) -> None:
    empty = {"minutes": ZERO, "completions": ZERO, "estimated": 0, "unknown_duration": 0}
    assert balance(orchard) == {"last_7_days": empty, "last_30_days": empty}
    activity = orchard.get_recent_activity()
    assert activity["balance"]["last_7_days"] == empty


def test_grouping_windows_and_the_minutes_rules(orchard: Orchard) -> None:
    done(orchard, 1, "want", actual=60)
    done(orchard, 2, "must", estimate=30)  # no actual: the estimate, counted as estimated
    done(orchard, 3, None)  # neither: 0 minutes, unknown duration
    done(orchard, 6.9, "should", actual=20, estimate=90)  # the actual wins
    done(orchard, 8, "want", actual=100)  # 30-day window only
    done(orchard, 29.9, "must", actual=10)
    done(orchard, 31, "must", actual=999)  # in neither
    week, month = balance(orchard)["last_7_days"], balance(orchard)["last_30_days"]
    assert week == {
        "minutes": {"want": 60, "should": 20, "must": 30, "unset": 0},
        "completions": {"want": 1, "should": 1, "must": 1, "unset": 1},
        "estimated": 1,
        "unknown_duration": 1,
    }
    assert month == {
        "minutes": {"want": 160, "should": 20, "must": 40, "unset": 0},
        "completions": {"want": 2, "should": 1, "must": 2, "unset": 1},
        "estimated": 1,
        "unknown_duration": 1,
    }


def test_the_window_edge(orchard: Orchard) -> None:
    done(orchard, 7, "want", actual=5)  # exactly seven days ago: inside
    assert balance(orchard)["last_7_days"]["completions"]["want"] == 1


def test_the_motive_is_the_effective_one_at_completion(orchard: Orchard) -> None:
    game = orchard.create_task("Make my game", motive="want")["task"]["id"]
    level = orchard.create_task("Design level one", parent_id=game)["task"]["id"]
    orchard.complete_task(level, actual_minutes=45)
    orchard.update_task(game, motive="should")  # later changes do not rewrite history
    assert balance(orchard)["last_7_days"]["minutes"]["want"] == 45
