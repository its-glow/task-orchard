# Task Orchard

Task Orchard is an AI powered task and time management application with MCP server integration. Task Orchard’s core feature is the development and management of **task trees**, which are fractal representations of larger tasks that get procedurally broken up into smaller, completable, sometimes repeatable tasks.

It’s meant for:
- Contexts where there is significant uncertainty over the next task
- Managing a single person when multiple competing priorities exist
- Helping a person make the most of their time, energy, and focus
It’s not meant for:
- Solving a specific project’s implementation order
- Coordinating between multiple workers - people or AI agents
- Managing AI agents

Task Orchard will try to learn about the user, recording how tasks went in order to better predict how long tasks take, as well as learn user preferences to help direct the user in task completion and prioritization.

## Tasks

Tasks are the core of Task Orchard. Each task is saved and recorded in a database.

There is no specific size for a task. A task might be any of the following:
- A single, actionable item: “call the mechanic”
- A major work effort: “design, build, and deploy an observability stack for my application”
- A recurring chore: “I want to go running twice a week”
- An activity: “I would like to spend more time reading other team’s docs”

Tasks are inherently fractal. A task that is a single step and can be completed in a few hours or less is “achievable”. Tasks that are unachievable get broken up into multiple child tasks. This process is designed to recurse, breaking down tasks until they are achievable.

### Task Aliveness

Tasks track their deadline and a relative priority. Tasks also can have prerequisites, such as “do they have open tasks”, but also start date and user states and moods. The service should (without making an AI call) be able to pull potential tasks - anything that’s not blocked by child tasks or start date - and pass to the AI and/or user to winnow down as a list of options.

### Child Tasks and Dependencies

Tasks are blocked if they have open children. Tasks can also block each other arbitrarily, but this will require the ability to check for or handle loops. Children inherit the parent’s priority, but can also go above it; likewise a dependent shouldn’t be a lower priority than its dependency.

Consider - actual priority is calculated dynamically (so each task has an inherent priority, but then also a live priority based on what it blocks?)

### The Leading Edge

Non-achievable tasks are subdivided lazily. Once the potential task pool is pulled, any task that is non-achievable but likely to be selected due to priority is subdivided, creating children and checking if the children block each other or need to have an enforced order. This can recurse, if the children are still non-achievable but potential tasks.

The intent is that for complex tasks, there’s a defined leading edge, but much of the work stays undefined - as tasks are completed, more of the required work gets defined.

### Checking For Completion

When a child task is marked complete, if the parent has no other open children, it’s also checked for completeness.

This does not guarantee the parent is complete. For instance, recurring tasks may be implemented as non-completable, non-achievable tasks. When examined, they are non-achievable and thus spawn a child task, but completing the child task never closes the parent - only updates the “care about” date.

### Saving Completed Tasks

One of the major goals of the application is to dial estimates on completion time, how much energy it took / how difficult it was for the user, etc. To achieve this, completed tasks are saved and can be referenced. The AI should prompt the user to evaluate the task when completed, with the goal that the user get into the habit of closing the task with a bit of context. Ie: (“I wrote the new interface spec - it took longer than expected and was hard to focus on because there were so many open questions and I didn’t have a block of uninterrupted time”)

Completed tasks might get grouped together in the database? So instead of fifty "exercise" tasks, when a task completes you just update the existing one in the archive as “oh this completed fifty-six times, and it was put off thirty times” or something.

This design goal implies that tasks aren’t always like jira tickets, with a specific defined backlog > todo > in progress > done lifecycle, but something a bit more dynamic. Maybe we need a system or a feature for reviving completed tasks periodically or checking if they need review?

Also, the user can and should record a task as complete if they do it, even if it wasn’t already tracked in the application - the agent would need to determine if it corresponds to something extant (and thus gets marked done), is a copy of something complete (and thus gets updated), or a completely new task (in which case it 

### Tentative List Of Task Properties
- Title
- Description
- Priority
- Deadline
- Deadline firmness (taxes are due on a specific, known date; laundry can slide a few days; reading a book you want to read can slide forever even if you wanted to get to it this weekend)
- Difficulty
- Motivation required? Focus required? Energy required?
- Creation Date
- Expected “care about” date (do not think about this task before this date)
- Links (parents, blockers)
- Prerequisite conditions (does this need to be done when the weather is good? Does it require a specific mood for the user?)
- Notes

## Workflow

The general workflow with Task Orchard is that the user should over time be more comfortable offloading prioritization and tasking to it. It should be a place for both large efforts that require collaborative breakdown with the user heavily involved in planning, throwaway “i need to do this” reminders, and broader long term goals. The approach and presentation should be “this is something you cultivate, and in return it helps you decide what you’re working on when when need it.” 

### User Stories

### Expected Tools or Routes
- Create task
- Update task
- Mark task complete and check parent
- Get top n tasks (splitting as needed)
- Get critical tasks (defined as a combination of priority and deadline upcoming, even if they’re blocked)

### Expected Skills
- Introductions and first setup
- Subdivide task (maybe this is a tool? This may or may not need user interaction)
- Plan time block (given constraints and inputs)

## AI’s Role

These are goals. On a long enough time frame, the AI should have enough information in the task history and other memory that it can perform the following:
- Task categorization and prioritization: AI only, with only occasional clarification from the user
- Task subdivision: AI only for known or obvious cases, but for complicated or ill-defined tasks the AI should present options and get review, approval, and updates from the user
- New task creation: user driven
- Task completion: user driven

## Invocations

Task Orchard is meant to enable an AI to provide guidance to a human. This is intended to run in three ways: via MCP, via an existing Agent platform, or via a full application.

### MCP

Task Orchard provides an MCP server. This serves a few purposes:
- MCP invocation is genuinely useful
- MCP invocation enables agent deployment, listed below
- In the prototype phase, letting an existing MCP client handle interface is a useful shortcut

It is expected that for basic usage, Task Orchard will be embedded in a simple MCP client (such as mcp-cli, crush, or goose) and the user will interface with it on the user’s schedule, terms, and prompting. For instance, the user would pop open a terminal window and be able to have a short conversation, updating tasks and getting their next priorities.

### Agent

Embedding Task Orchard in an agent (Hermes, OpenClaw) lets it check in on the user for status, plan days in the morning and perform reflections at night.

### Application

In a full rollout, the primary interface is an android application that will both make non-mcp calls of the service to pull info, data, etc into the user interface, but also have a channel to talk to the agent to make plans, get reminders, and set timers.

## Structure

This section is exceptionally tentative and subject to change. However, the current plan is a sqlite database, python backend and fastapi and fastmcp interfaces.
