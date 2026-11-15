"""Derived state, computed on read and never stored.

PHASE 1: open, dormant, blocked, has_open_children, candidate.
PHASE 2: effective priority, effective deadline (any and hard), effective
motive, remaining minutes, at-risk, each with where it came from.

Load all open tasks and their links once into an in-memory snapshot, then
answer every question from that. One user's data is small; do not cache
between requests. SPEC section 5.
"""
