"""SPEC 8.17 and 6.1 (Phase 3 digest): find_patterns, save_pattern."""

from __future__ import annotations

import pytest

from task_orchard.core.errors import Invalid, NotFound
from task_orchard.core.service import Orchard
from tests.helpers import counts, events, last_event_id, make


def create(orchard: Orchard, name: str, **kw: str) -> int:
    return orchard.save_pattern(name=name, **kw)["pattern"]["id"]


def test_create_returns_the_digest(orchard: Orchard) -> None:
    result = orchard.save_pattern(
        name="  Phone   calls ", description="Booking things", learned="Hard to start."
    )
    assert result["pattern"] == {
        "id": result["pattern"]["id"],
        "name": "Phone calls",
        "description": "Booking things",
        "learned": "Hard to start.",
    }
    pattern_id = result["pattern"]["id"]
    assert events(orchard)[-1] == (
        "pattern_saved",
        None,
        {
            "pattern_id": pattern_id,
            "created": True,
            "changes": {
                "name": {"from": None, "to": "Phone calls"},
                "learned": {"from": None, "to": "Hard to start."},
                "description": {"from_chars": 0, "to_chars": 14},
            },
        },
    )


def test_a_new_pattern_needs_a_name(orchard: Orchard) -> None:
    for kw in ({}, {"name": "  "}, {"learned": "x"}):
        with pytest.raises(Invalid):
            orchard.save_pattern(**kw)


def test_name_clash_ignores_case_and_names_the_existing_pattern(orchard: Orchard) -> None:
    existing = create(orchard, "Phone calls")
    before = counts(orchard)
    with pytest.raises(Invalid) as caught:
        orchard.save_pattern(name="PHONE CALLS")
    assert caught.value.details["pattern_id"] == existing
    assert counts(orchard) == before


def test_rename_and_change(orchard: Orchard) -> None:
    pattern = create(orchard, "Calls", learned="Old note.")
    since = last_event_id(orchard)
    result = orchard.save_pattern(pattern, name="Phone calls", learned="New note.")
    assert result["pattern"]["name"] == "Phone calls"
    assert result["pattern"]["learned"] == "New note."
    assert events(orchard, since) == [
        (
            "pattern_saved",
            None,
            {
                "pattern_id": pattern,
                "changes": {
                    "name": {"from": "Calls", "to": "Phone calls"},
                    "learned": {"from": "Old note.", "to": "New note."},
                },
            },
        )
    ]


def test_changing_case_of_its_own_name_is_not_a_clash(orchard: Orchard) -> None:
    pattern = create(orchard, "phone calls")
    assert orchard.save_pattern(pattern, name="Phone calls")["pattern"]["name"] == "Phone calls"


def test_renaming_onto_another_pattern_is_a_clash(orchard: Orchard) -> None:
    create(orchard, "Calls")
    other = create(orchard, "Emails")
    with pytest.raises(Invalid):
        orchard.save_pattern(other, name="calls")


def test_blank_text_clears_and_saving_the_same_changes_nothing(orchard: Orchard) -> None:
    pattern = create(orchard, "Calls", learned="A note.")
    assert "learned" not in orchard.save_pattern(pattern, learned="  ")["pattern"]
    before = counts(orchard)
    orchard.save_pattern(pattern, name="Calls")
    assert counts(orchard) == before


def test_learned_limit(orchard: Orchard) -> None:
    pattern = create(orchard, "Calls")
    orchard.save_pattern(pattern, learned="x" * 600)
    with pytest.raises(Invalid) as caught:
        orchard.save_pattern(pattern, learned="x" * 601)
    assert "shorter" in caught.value.hint


def test_unknown_pattern(orchard: Orchard) -> None:
    with pytest.raises(NotFound):
        orchard.save_pattern(99, learned="x")


def test_digest_counts_members_by_status(orchard: Orchard, seeded: dict[str, int]) -> None:
    specs = orchard.get_task(seeded["obs_spec"])["task"]["pattern"]
    assert specs["name"] == "Interface specs"
    assert specs["open"] == 1
    assert specs["done"] == 1  # the migration spec
    assert "dropped" not in specs


def test_recurring_tasks_are_not_members(orchard: Orchard, seeded: dict[str, int]) -> None:
    # The weekly summary and its instances share a pattern; only instances count.
    summary = orchard.get_task(seeded["summary_w3"])["task"]["pattern"]
    assert summary["open"] == 1  # summary_w3; the recurring task itself is not counted
    assert summary["missed"] == 1


def test_merge_moves_tasks_and_deletes_the_source(orchard: Orchard) -> None:
    keep = create(orchard, "Phone calls")
    fold = create(orchard, "Calls to book", learned="Old.")
    a = make(orchard, pattern_id=fold)
    b = make(orchard, pattern_id=fold)
    since = last_event_id(orchard)
    result = orchard.save_pattern(keep, merge_from_id=fold, learned="Combined note.")
    assert result["pattern"]["open"] == 2
    assert result["pattern"]["learned"] == "Combined note."
    assert orchard.get_task(a)["task"]["pattern"]["id"] == keep
    assert orchard.get_task(b)["task"]["pattern"]["id"] == keep
    with pytest.raises(NotFound):
        orchard.save_pattern(fold, learned="x")
    assert [e[0] for e in events(orchard, since)] == ["patterns_merged", "pattern_saved"]
    assert events(orchard, since)[0][2] == {
        "from": {"id": fold, "name": "Calls to book"},
        "into": keep,
        "tasks_moved": 2,
    }


def test_merge_may_take_the_source_name(orchard: Orchard) -> None:
    keep = create(orchard, "Calls")
    fold = create(orchard, "Phone calls")
    result = orchard.save_pattern(keep, merge_from_id=fold, name="Phone calls")
    assert result["pattern"]["name"] == "Phone calls"


def test_merge_rules(orchard: Orchard) -> None:
    keep = create(orchard, "Calls")
    with pytest.raises(Invalid):
        orchard.save_pattern(keep, merge_from_id=keep)
    with pytest.raises(Invalid):
        orchard.save_pattern(name="New", merge_from_id=keep)
    with pytest.raises(NotFound):
        orchard.save_pattern(keep, merge_from_id=99)


def test_find_by_words(orchard: Orchard, seeded: dict[str, int]) -> None:
    found = orchard.find_patterns("phone")["patterns"]
    assert [p["name"] for p in found] == ["Phone calls to book things"]
    found = orchard.find_patterns("uninterrupted morning")["patterns"]
    assert [p["name"] for p in found] == ["Interface specs"]  # matched in learned
    assert orchard.find_patterns("zebra")["patterns"] == []
    with pytest.raises(Invalid):
        orchard.find_patterns('"*"')


def test_find_with_no_query_lists_most_recently_used_first(orchard: Orchard) -> None:
    old = create(orchard, "Old")
    recent = create(orchard, "Recent")
    unused = create(orchard, "Unused")
    orchard.complete_task(make(orchard, pattern_id=old), completed_at="2026-03-10")
    orchard.complete_task(make(orchard, pattern_id=recent), completed_at="2026-03-17")
    names = [p["name"] for p in orchard.find_patterns()["patterns"]]
    assert names == ["Recent", "Old", "Unused"]
    assert orchard.find_patterns(limit=1)["patterns"][0]["id"] == recent
    with pytest.raises(Invalid):
        orchard.find_patterns(limit=11)
    assert unused


def test_a_description_is_at_most_600_characters(orchard: Orchard) -> None:
    orchard.save_pattern(name="Short", description="d" * 600)
    with pytest.raises(Invalid):
        orchard.save_pattern(name="Long", description="d" * 601)
