"""SPEC 6.2 and 8.18: the profile and the charter."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from task_orchard.core.errors import Invalid, ProfileFull
from task_orchard.core.service import Orchard
from task_orchard.db.models import Event


def profile_events(orchard: Orchard) -> list[dict]:
    with orchard.tx() as tx:
        return [
            e.payload
            for e in tx.session.scalars(
                sa.select(Event).where(Event.type == "profile_saved").order_by(Event.id)
            )
        ]


def test_an_empty_profile(orchard: Orchard) -> None:
    result = orchard.read_profile()
    assert result["notes"] == []
    assert result["size"] == {"used": 0, "limit": 24000}
    assert "charter" not in result


def test_upsert_with_case_insensitive_topics(orchard: Orchard) -> None:
    orchard.write_profile_note("Energy", "Low after 3pm.")
    result = orchard.write_profile_note("  energy ", "Low after 3pm on weekdays.")
    assert [(n["topic"], n["text"]) for n in result["notes"]] == [
        ("energy", "Low after 3pm on weekdays.")
    ]
    assert result["size"]["used"] == len("Low after 3pm on weekdays.")
    assert profile_events(orchard) == [{"topic": "Energy"}, {"topic": "energy"}]
    # The same text again changes nothing and writes nothing.
    orchard.write_profile_note("energy", "Low after 3pm on weekdays.")
    assert len(profile_events(orchard)) == 2


def test_notes_are_ordered_by_topic_and_the_charter_is_apart(orchard: Orchard) -> None:
    orchard.write_profile_note("work", "Remote on Fridays.")
    orchard.write_profile_note("Chores", "None before noon.")
    result = orchard.write_profile_note("CHARTER", "Keep work from eating everything.")
    assert [n["topic"] for n in result["notes"]] == ["Chores", "work"]
    assert result["charter"]["text"] == "Keep work from eating everything."
    assert result["charter"]["updated"]["rel"] == "now"
    assert result["size"]["used"] == len("Remote on Fridays.") + len("None before noon.")


def test_blank_text_deletes(orchard: Orchard) -> None:
    orchard.write_profile_note("chores", "None before noon.")
    result = orchard.write_profile_note("Chores", "   ")
    assert result["notes"] == []
    assert profile_events(orchard)[-1] == {"topic": "chores", "deleted": True}
    orchard.write_profile_note("never written", "")  # nothing to delete: no event
    assert len(profile_events(orchard)) == 2


def test_seeded_profile(orchard: Orchard, seeded: dict[str, int]) -> None:
    result = orchard.read_profile()
    assert "my game" in result["charter"]["text"]
    assert [n["topic"] for n in result["notes"]] == ["chores", "energy"]


def test_limits(orchard: Orchard) -> None:
    orchard.write_profile_note("charter", "c" * 4000)
    with pytest.raises(ProfileFull) as caught:
        orchard.write_profile_note("Charter", "c" * 4001)
    assert caught.value.details["charter_limit"] == 4000
    assert caught.value.details["charter_used"] == 4000  # the current sizes
    assert caught.value.details["notes_used"] == 0
    assert "Consolidate" in caught.value.hint

    orchard.write_profile_note("one", "n" * 2000)
    with pytest.raises(ProfileFull):
        orchard.write_profile_note("two", "n" * 2001)

    for n in range(2, 12):
        orchard.write_profile_note(f"topic {n}", "n" * 2000)
    assert orchard.read_profile()["size"] == {"used": 22000, "limit": 24000}
    orchard.write_profile_note("last", "n" * 2000)
    with pytest.raises(ProfileFull) as caught:
        orchard.write_profile_note("one more", "n")
    assert caught.value.details["notes_used"] == 24000
    # Replacing a note counts its new size, not both.
    orchard.write_profile_note("last", "n" * 1900)
    orchard.write_profile_note("one more", "n" * 100)


def test_topics_are_checked(orchard: Orchard) -> None:
    for topic in ("", "   ", "t" * 101):
        with pytest.raises(Invalid):
            orchard.write_profile_note(topic, "text")
