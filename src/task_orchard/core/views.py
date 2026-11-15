"""Builders for the JSON the tools return.

PHASE 1 onward. SPEC section 4. Three shapes and nothing else:

- task_row: the compact row used in every list;
- task_detail: one task in full, for get_task and write responses;
- envelope: the wrapper every response shares (`now`, plus the payload).

Keys whose value is null, empty or false are omitted, to save context.
"""
