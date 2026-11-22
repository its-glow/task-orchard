"""SPEC 10.4: `task-orchard dump`, every task as CSV, and --help on every command."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from task_orchard import cli
from task_orchard.core.dump import columns
from task_orchard.core.enums import TRACKED_FIELDS
from task_orchard.core.service import Orchard


def by_id(rows):
    return {int(r["id"]): r for r in rows}


def test_every_task_is_dumped_by_default(orchard: Orchard, seeded) -> None:
    rows = orchard.dump()
    assert list(rows[0]) == columns(sources=False)
    ids = [int(r["id"]) for r in rows]
    assert ids == sorted(ids)
    rows = by_id(rows)
    assert rows[seeded["obs_backend"]]["status"] == "done"
    assert rows[seeded["obs_backend"]]["closed"]
    assert set(rows) >= set(seeded.values())


def test_open_only(orchard: Orchard, seeded) -> None:
    rows = by_id(orchard.dump(open_only=True))
    assert seeded["obs_backend"] not in rows
    assert {r["status"] for r in rows.values()} == {"open"}


def test_stored_and_computed_values(orchard: Orchard, seeded) -> None:
    rows = by_id(orchard.dump())
    cert = rows[seeded["cert"]]
    assert cert["priority"] == "low"
    assert cert["effective_priority"] == "critical"  # it blocks the release
    assert cert["effective_firmness"] == "hard" and cert["effective_deadline"]
    assert cert["deadline"] == ""
    assert rows[seeded["release_notes"]]["blocked"] == "yes"
    assert rows[seeded["release_flaky"]]["blocked"] == "no"
    assert rows[seeded["release_flaky"]]["candidate"] == "yes"
    assert rows[seeded["taxes"]]["dormant"] == "yes"
    assert rows[seeded["milk"]]["overdue"] == "yes"
    assert rows[seeded["inbox_desk"]]["inbox"] == "yes"
    assert rows[seeded["game_movement"]]["effective_motive"] == "want"
    assert rows[seeded["summary"]]["recurrence"] == "scheduled"
    assert "FREQ=WEEKLY" in rows[seeded["summary"]]["rule"]
    assert rows[seeded["laundry"]]["recurrence"] == "flexible"
    assert rows[seeded["laundry"]]["gap_days"] == "5"
    # Computed values describe open tasks only.
    done = rows[seeded["obs_backend"]]
    assert all(done[c] == "" for c in ("blocked", "dormant", "candidate", "overdue"))


def test_sources_add_a_column_per_tracked_field(orchard: Orchard) -> None:
    task = orchard.create_task(
        "Book the MOT", priority="high", estimate_minutes=20, sources={"priority": "user"}
    )["task"]["id"]
    assert list(orchard.dump(sources=True)[0]) == columns(sources=True)
    row = by_id(orchard.dump(sources=True))[task]
    assert [f"{f}_source" for f in TRACKED_FIELDS] == columns(sources=True)[-len(TRACKED_FIELDS) :]
    assert row["priority_source"] == "user"
    assert row["estimate_minutes_source"] == "guess"
    assert row["drain_source"] == ""  # no value, so no source


def run_cli(argv, monkeypatch, capsys, db: Path) -> tuple[int, str, str]:
    monkeypatch.setenv("TASK_ORCHARD_DB", str(db))
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_cli_writes_csv_to_stdout(monkeypatch, capsys, db_path: Path) -> None:
    orchard = Orchard.open(cli.load_settings({"TASK_ORCHARD_DB": str(db_path)}))
    orchard.create_task('Call Sam, then "Jo"', estimate_minutes=5)
    code, out, err = run_cli(["dump", "--open", "--sources"], monkeypatch, capsys, db_path)
    assert code == 0 and err == ""
    rows = list(csv.DictReader(io.StringIO(out)))
    assert rows[-1]["title"] == 'Call Sam, then "Jo"'  # quoted, so commas survive
    assert "priority_source" in rows[0]
    assert len(out.splitlines()) == len(rows) + 1  # one line per task


def test_an_empty_database_dumps_the_header(monkeypatch, capsys, tmp_path: Path) -> None:
    code, out, _ = run_cli(["dump"], monkeypatch, capsys, tmp_path / "empty.db")
    assert code == 0
    assert out.strip() == ",".join(columns(sources=False))


@pytest.mark.parametrize("command", [[], *([name] for name in cli.COMMANDS)])
def test_every_command_takes_help(command, capsys) -> None:
    with pytest.raises(SystemExit) as done:
        cli.main([*command, "--help"])
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: task-orchard")
    assert "--help" in out
