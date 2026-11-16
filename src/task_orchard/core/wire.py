"""Reading arguments in their wire form (SPEC 4.1) into stored values.

Every check here raises Invalid with a hint, so a small model can correct the
call. Enumerated values are matched leniently: case is ignored, surrounding
whitespace trimmed, and spaces and hyphens count as underscores.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Any

from task_orchard.core.enums import TRACKED_FIELDS, FieldSource
from task_orchard.core.errors import Invalid

TITLE_MAX_CHARS = 500
NOTES_MAX_CHARS = 20_000
CONDITIONS_MAX = 5
CONDITION_MAX_CHARS = 30
CAPTURE_MAX_ITEMS = 20


def _key(value: str) -> str:
    return "_".join(value.strip().lower().replace("-", " ").split())


def enum_name(member: Enum) -> str:
    """The wire name of a member: the lowercase name of a scale, the value of the rest."""
    label = getattr(member, "label", None)
    return label if isinstance(label, str) else str(member.value)


def parse_enum[E: Enum](cls: type[E], value: Any, param: str) -> E:
    if isinstance(value, cls):
        return value
    names = [enum_name(m) for m in cls]
    if isinstance(value, str):
        key = _key(value)
        for member, name in zip(cls, names, strict=True):
            if name == key:
                return member
    allowed = ", ".join(names)
    raise Invalid(
        f"{param} must be one of: {allowed}. Got {value!r}.",
        hint=f"Pass {param} as one of these names: {allowed}.",
    )


def title(value: Any, *, param: str = "title") -> str:
    if not isinstance(value, str) or not value.strip():
        raise Invalid(f"{param} must be non-blank text.", hint="Give the task a short title.")
    text = " ".join(value.split())
    if len(text) > TITLE_MAX_CHARS:
        raise Invalid(
            f"{param} is {len(text)} characters; the limit is {TITLE_MAX_CHARS}.",
            hint="Keep the title short and put the rest in description or notes.",
        )
    return text


def text(value: Any, param: str) -> str:
    if not isinstance(value, str):
        raise Invalid(f"{param} must be text.", hint=f"Pass {param} as a string.")
    return value


def check_notes_length(notes: str | None) -> None:
    if notes is not None and len(notes) > NOTES_MAX_CHARS:
        raise Invalid(
            f"notes would be {len(notes)} characters; the limit is {NOTES_MAX_CHARS}.",
            hint="Rewrite the notes shorter with update_task(notes=...), keeping what matters.",
        )


def positive_int(value: Any, param: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Invalid(f"{param} must be a whole number above 0.", hint=f"For example {param}=30.")
    return value


def conditions(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise Invalid(
            "conditions must be a list of tags.",
            hint='For example conditions=["at-home", "good-weather"].',
        )
    out: list[str] = []
    for tag in value:
        if not isinstance(tag, str):
            raise Invalid("Each condition must be text.", hint='For example "at-home".')
        cleaned = "-".join(tag.strip().lower().split())
        if len(cleaned) > CONDITION_MAX_CHARS:
            raise Invalid(
                f"The condition {cleaned[:40]!r} is longer than {CONDITION_MAX_CHARS} characters.",
                hint='A condition is a short tag, such as "at-home" or "good-weather".',
            )
        if cleaned and cleaned not in out:
            out.append(cleaned)
    if len(out) > CONDITIONS_MAX:
        raise Invalid(
            f"{len(out)} conditions given; a task has at most {CONDITIONS_MAX}.",
            hint="Keep the conditions that really decide when it can be done.",
        )
    return out


def ids(value: Any, param: str) -> list[int]:
    if value is None:
        return []
    if isinstance(value, int):  # a single id (bools are ints too, rejected below)
        value = [value]
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise Invalid(f"{param} must be a list of task ids.", hint=f"For example {param}=[12, 15].")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise Invalid(
                f"{param} must contain task ids (numbers).", hint="Ids come from results."
            )
        if item not in out:
            out.append(item)
    return out


def sources(value: Any) -> dict[str, FieldSource]:
    """The `sources` object of SPEC 4.5. `firmness` is accepted as `deadline`.

    An entry for a field the call does not write is ignored; an entry for a
    field that is not tracked is invalid, since it is almost always a typo.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Invalid("sources must be an object.", hint='For example {"deadline": "user"}.')
    out: dict[str, FieldSource] = {}
    tracked = ", ".join(TRACKED_FIELDS)
    for field, source in value.items():
        name = _key(str(field))
        if name in ("firmness", "deadline_firmness"):
            name = "deadline"
        if name not in TRACKED_FIELDS:
            raise Invalid(
                f"sources names {field!r}, which is not a tracked field.",
                hint=f"Tracked fields: {tracked}.",
            )
        out[name] = parse_enum(FieldSource, source, f"sources.{name}")
    return out


def names(value: Any, allowed: Iterable[str], param: str) -> list[str]:
    allowed = list(allowed)
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    out: list[str] = []
    for item in value:
        name = _key(str(item))
        if name not in allowed:
            raise Invalid(
                f"{param} cannot include {item!r}.",
                hint=f"{param} takes these names: {', '.join(allowed)}.",
            )
        if name not in out:
            out.append(name)
    return out
