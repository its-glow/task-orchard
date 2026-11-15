"""The must-finish-first graph: blocking, inherited blocking and cycle checks.

PHASE 1. Pure functions over plain data (ids and edges), with no database
access, so they can be property-tested directly. SPEC section 5.1 and 5.2.

To implement:

- build the effective edge set: child -> parent, blocker -> blocked, and
  blocker -> every descendant of blocked;
- find_cycle(edges): return the loop as a list of ids, or None;
- check a proposed link, move or subdivision by applying it to the edge set
  and looking for a cycle, raising CycleError with a readable path.
"""
