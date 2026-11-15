"""What the model reads about the past: digests, counts and the balance.

PHASE 3: per-task counts (deferred, starts, last touched).
PHASE 5: pattern digests, the want / should / have-to balance, the review
queue and the recent-activity feed.

Everything here is aggregated on read from completions and events. Raw rows
never leave this module. SPEC section 6.
"""
