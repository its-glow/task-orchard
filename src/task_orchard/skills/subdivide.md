---
name: subdivide
description: Break a task that is too big or too vague into its next layer of steps. One layer only, in one subdivide_task call.
---
# Subdivide

Goal: a task that cannot be worked on as it stands gets 2 to 7 children, each of which is either one clear step or obviously needs its own breakdown later. Only the next layer is planned; the rest stays undefined until work gets there.

Use this when a task has `achievable` of `no` or `unknown` and the user wants to work on it, or when the user asks to break something down.

## Steps

1. Call `get_task` on the task. Read its title, description, notes, children and pattern. If it already has open children, the new steps go alongside them, so do not repeat them.
2. If the user's profile is not already in front of you, call `read_profile`. If you have `find_patterns`, call it with words from the title. A pattern's `learned` note often says how this kind of work goes.
3. **Draft one layer** of 2 to 7 children.
   - Each child is one step of a few hours or less (`achievable: "yes"`), or a clearly bounded piece that will be broken down later (`achievable: "no"`). Leave `achievable` out if you cannot tell.
   - Start each title with a verb: "Email Sam for the venue list", not "Venue".
   - Make the first step small and concrete, so starting is easy.
   - Do not plan the whole tree. A vague later step stays one vague child.
4. **Fill each child's fields by guessing.** Use the parent, the pattern and the profile for `estimate_minutes`, `activation`, `drain`, `priority` and `conditions`. Leave a field out when you have no basis for it. Children inherit the parent's priority, deadline and motive, so set those only where a child differs.
5. **Order only where it is real.** If each step truly waits for the one before, pass `sequential: true`. If only some do, give those children `after` with the positions (counting from 1) of the steps they wait for. Otherwise give no order: unordered steps can be done in any order, which is what you want.
6. **Act or propose.**
   - If the breakdown is obvious (the steps follow plainly from the title, or the user listed them), call `subdivide_task` straight away.
   - If it is not obvious (the goal is vague, or there are real choices about approach), show the user the draft as a short numbered list and ask one question: "Does this look right?" Then call `subdivide_task` with their corrections.
7. **Call `subdivide_task` once** with all the children. Mark values the user stated with `sources` (it applies to every child), and leave your own guesses out of it.
8. **Report briefly**: the steps, and which one to start with. If the first step has high `activation`, suggest the smallest way into it.

## When to stop

- One layer per call. You may break down one of the new children in the same turn if the user asks, but never more than two levels in one turn.
- If a child is still vague, leave it vague. It will be broken down when it is next.

## Do not

- Create more than 7 children. If there seem to be more, group them under a few broader children.
- Ask about each child's fields. Guess, and say what you assumed if it matters.
- Use `create_task` repeatedly to add children. `subdivide_task` does it in one atomic call.
