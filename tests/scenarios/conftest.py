"""Scenarios call a real model. They are marked `scenario`, excluded from the
normal run, and skipped when there are no credentials. They cost money: ask the
user before running them. Run with `uv run pytest -m scenario -s`.
"""

from __future__ import annotations

import pytest

from task_orchard.agent.model import has_credentials


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "scenarios" in item.path.parts:
            item.add_marker(pytest.mark.scenario)


@pytest.fixture(autouse=True)
def _needs_credentials() -> None:
    if not has_credentials():
        pytest.skip("no Claude API credentials")
