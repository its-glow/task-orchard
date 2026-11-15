"""Loading skills from the markdown files in task_orchard/skills.

A skill is one markdown file with a small header:

    ---
    name: what-now
    description: One sentence saying when to use this.
    ---
    The instructions, written to the agent.

The same file is served three ways (SPEC section 9): as an MCP prompt, through
the get_guide tool, and (by copying the file) as a SKILL.md for agent
platforms that load skills from disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib import resources


@dataclass(frozen=True)
class Guide:
    name: str
    description: str
    body: str


class GuideFormatError(ValueError):
    pass


def parse_guide(text: str, source: str = "<guide>") -> Guide:
    """Parse one skill file. The header is plain `key: value` lines, not YAML."""
    if not text.startswith("---\n"):
        raise GuideFormatError(f"{source}: must start with a '---' header line")
    try:
        header, body = text[4:].split("\n---\n", 1)
    except ValueError:
        raise GuideFormatError(f"{source}: header is not closed by a '---' line") from None
    fields: dict[str, str] = {}
    for line in header.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise GuideFormatError(f"{source}: header line {line!r} is not 'key: value'")
        fields[key.strip()] = value.strip()
    missing = {"name", "description"} - fields.keys()
    if missing:
        raise GuideFormatError(f"{source}: header is missing {sorted(missing)}")
    if not body.strip():
        raise GuideFormatError(f"{source}: has no body")
    return Guide(name=fields["name"], description=fields["description"], body=body.strip() + "\n")


@cache
def load_guides() -> dict[str, Guide]:
    """Every skill shipped in the package, by name."""
    guides: dict[str, Guide] = {}
    root = resources.files("task_orchard") / "skills"
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".md"):
            continue
        guide = parse_guide(entry.read_text(encoding="utf-8"), source=entry.name)
        if guide.name in guides:
            raise GuideFormatError(f"{entry.name}: duplicate skill name {guide.name!r}")
        guides[guide.name] = guide
    return guides
