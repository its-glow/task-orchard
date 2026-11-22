---
name: plan-time-block
description: Plan a block of time ("I've got the afternoon", "plan my next two hours") as a short ordered list that fits, with times.
---
# Plan a time block

Goal: an ordered plan for one block of time that fits inside it with room to spare, starts with something hard while energy is fresh, and keeps the charter's balance.

## Steps

1. **The block.** Take its length and the user's energy from what they said. If the length is missing, ask once. Energy is optional; leave it out unless they mentioned it.
2. **The facts.** Call `get_candidates` with `minutes_available` set to the block length, `energy` if known, and `limit: 15`. Call `get_recent_activity` with `days: 7` for the balance. If the profile is not already in front of you, call `read_profile` for the charter.
3. **Build the plan.**
   - Fill about four fifths of the block. Leave the last fifth as buffer.
   - Use each row's `estimate_minutes`. If its pattern hint says work of that kind runs long, plan for that. If a row has no estimate, guess one.
   - Put one high-activation task early, while energy is fresh. Follow it with lighter ones.
   - Give wants a share as the charter directs. If the balance shows few `want` minutes this week, include one.
   - Prefer tasks with a deadline, then what unblocks others, then the rest.
   - Respect `blocked`: a blocked row cannot start until its blocker is done. Plan the blocker first or leave both out.
   - Three to six items is usually right.
4. **Present it** as a numbered list with a start time for each item, counting from the time in the latest Now line, plus a one-line reason where the choice is not obvious. End with the buffer: "15 minutes spare".
5. If the user accepts and the first task is not under way, offer to start it with `start_task`.

## Do not

- Create, change or snooze tasks while planning. A plan is a suggestion.
- Fill the whole block. Things take longer than planned.
- Put two high-activation tasks back to back.
- Plan a task whose `achievable` is `no` or `unknown`. Offer to break it down with the `subdivide` guide instead.
