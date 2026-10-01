---
type: skill
name: okf-tasks
description: "Durable cross-session task tracking for OKF vault. Lifecycle: open → in-progress → done/cancelled."
---

# okf-tasks

Durable cross-session task tracking for the OKF vault.

## Trigger

When creating, listing, updating, or closing tasks; when work won't finish this
session; when user mentions tasks, todo, work items, action items in the
context of the vault.

## Directory layout

```
tasks/
├── _template.md          # blank task template
├── open/                 # new tasks land here
├── in-progress/          # claimed/active tasks
├── done/YYYY/MM/         # completed tasks, nested by close date
└── cancelled/YYYY/MM/    # cancelled tasks, nested by cancel date
```

## Task frontmatter schema

| Key               | Type        | Notes                                         |
|--------------------|-------------|-----------------------------------------------|
| `id`              | string      | `tsk-YYYY-MM-DD-NNN` (date = creation date)  |
| `title`           | string      | human-readable summary                        |
| `assignee`        | string      | `unassigned` or agent/person identifier       |
| `priority`        | int 1–5     | 1 = highest, 5 = lowest                      |
| `status`          | enum        | `open`, `in-progress`, `done`, `cancelled`   |
| `blocked_reason`  | string|null | why this task is stalled                      |
| `created`         | ISO 8601    | creation timestamp                            |
| `updated`         | ISO 8601    | last meaningful change                        |
| `due`             | ISO 8601|null | optional deadline                           |
| `created_by`      | string      | `self` or agent identifier                    |
| `parent`          | string|null | id of parent task for subtasks                |
| `linked_concepts` | list        | concept paths without `.md` extension         |
| `linked_skills`   | list        | skill identifiers                             |
| `tags`            | list        | lowercase, hyphenated                         |

## Procedures

### CREATE

1. Check for duplicates — `grep` task `title` across `tasks/open/` and `tasks/in-progress/`.
2. Generate id: `tsk-YYYY-MM-DD-NNN` where `NNN` is the next sequential number for that date (scan existing files).
3. Copy `_template.md` to `tasks/open/tsk-YYYY-MM-DD-NNN.md`.
4. Fill frontmatter: `title`, `created`, `updated` (now), `tags`, `assignee`, `priority`.
5. Populate `linked_concepts`: search the vault for concept files relevant to the task topic. Store paths relative to `concepts/` without the `.md` extension.
6. Populate `linked_skills`: identify applicable skill identifiers from `skills/`.
7. Fill body: `What this is`, `Context` (add markdown links to linked concepts/skills), `Success criteria`.
8. Add first update line: `- YYYY-MM-DD (who) — created`.
9. Verify the file parses (valid frontmatter).

### LIST

1. Walk `tasks/open/` and `tasks/in-progress/` — read frontmatter from each `.md` file (skip `.gitkeep`).
2. Print summary table: `id | priority | status | title | assignee`.
3. Optionally filter by `assignee`, `tag`, `priority`, or `status`.

### CLAIM

1. Locate the task file in `tasks/open/`.
2. Move to `tasks/in-progress/`.
3. Set `status: in-progress`, update `assignee`, update `updated` timestamp.
4. Add update line: `- YYYY-MM-DD (who) — claimed, moved to in-progress`.

### CLOSE

1. Locate the task file in `tasks/in-progress/`.
2. Create target directory: `tasks/done/YYYY/MM/` (based on close date).
3. Move the file there.
4. Set `status: done`, update `updated` timestamp.
5. Write `## Outcome` section with the result.
6. Add update line: `- YYYY-MM-DD (who) — closed, moved to done/YYYY/MM/`.

### CANCEL

1. Locate the task file in `tasks/open/` or `tasks/in-progress/`.
2. Create target directory: `tasks/cancelled/YYYY/MM/` (based on cancel date).
3. Move the file there.
4. Set `status: cancelled`, update `updated` timestamp.
5. Write `## Outcome` section with the cancellation reason.
6. Add update line: `- YYYY-MM-DD (who) — cancelled, moved to cancelled/YYYY/MM/`.

### BLOCK / UNBLOCK

1. Locate the task file.
2. BLOCK: set `blocked_reason` to a description of the blocker, add update line.
3. UNBLOCK: clear `blocked_reason` (set to `null`), add update line.

## Rules

- Tasks are **vault-level durable** — unlike the harness todo tool which is session-scoped, task files persist across sessions and are committed to the repo.
- Never auto-create stub concept files for concepts that don't exist yet. If a task references a concept that hasn't been written, note it in `Context` as a plain-text reference.
- `linked_concepts` stores concept paths **without** the `.md` extension (e.g., `tools/nub`, not `tools/nub.md`).
- `linked_skills` stores skill folder names (e.g., `okf-ingest`, not `skills/okf-ingest/SKILL.md`).
- Task ids must be unique within the vault. If a date collision occurs, increment `NNN`.
- The `## Outcome` section is filled only when status flips to `done` or `cancelled`.
- When moving a task, use filesystem move (not copy + delete) to preserve git history.
