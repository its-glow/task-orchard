"""Column types shared by the models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum

import sqlalchemy as sa
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """An instant, stored as naive UTC and always returned aware.

    SQLite has no timezone-aware type. Binding a naive datetime is a bug, so it
    raises instead of guessing.
    """

    impl = sa.DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTCDateTime needs an aware datetime; got a naive one")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


class IntEnumType(TypeDecorator[IntEnum]):
    """An IntEnum stored as its integer, so the database can order by it."""

    impl = sa.Integer
    cache_ok = True

    def __init__(self, enum_cls: type[IntEnum]) -> None:
        super().__init__()
        self.enum_cls = enum_cls

    def process_bind_param(self, value: IntEnum | int | None, dialect: sa.Dialect) -> int | None:
        return None if value is None else int(self.enum_cls(value))

    def process_result_value(self, value: int | None, dialect: sa.Dialect) -> IntEnum | None:
        return None if value is None else self.enum_cls(value)


def str_enum(enum_cls: type[StrEnum], name: str, *, constraint: bool = True) -> sa.Enum:
    """A StrEnum stored as its value.

    By default the column gets a CHECK constraint listing the values. Pass
    constraint=False for enums expected to grow (event types), so that adding a
    member does not need a table rebuild; Python still validates the value.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=constraint,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
