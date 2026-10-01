---
type: skill
name: okf-review
description: "Spaced-repetition review engine for OKF learning concepts. Triggered on 'spaced repetition', 'SRS session', 'what's due'."
---

# OKF Review Skill

## Trigger Patterns
- "review my learning"
- "spaced repetition"
- "recall session"
- "what should I review"
- "review session"
- "time to review"
- "/review"
- "/recall"

## Interval Schedule

The review engine tracks each concept's review interval via the `review_interval` frontmatter field (in days). The `timestamp` field records the last review date.

**Default progression:**

| Score | Action | Interval change |
|-------|--------|-----------------|
| ✅ Full recall | Advance to next interval | 1d → 3d → 7d → 14d → 30d |
| 🟡 Partial | Halve current interval | e.g. 7d → 3d, 14d → 7d |
| ❌ Failed | Reset to 1d, re-study now | Any → 1d |

The interval ladder: **New → 1d → 3d → 7d → 14d → 30d**

- If ✅ and already at 30d, stay at 30d (maintenance mode).
- If 🟡 halves to a non-ladder value (e.g. 3d → 1.5d), round down to the nearest ladder step (→ 1d).

**Frontmatter fields used:**
```yaml
review_interval: 3        # current interval in days (absent = new, treat as 1d)
timestamp: 2026-08-10     # last review date (ISO 8601 date)
```

## SESSION Procedure

A timed retrieval-practice session. Goal: recall from memory, not re-read.

### 1. Find stale concepts

```bash
# Concepts due for review (timestamp older than their interval)
python3 tools/okf.py search "domain:learning" --limit 50
```

Then filter: a concept is **due** when `today - timestamp >= review_interval`. For concepts without `review_interval`, treat as new (interval = 1d, always due).

Priority order:
1. ❌-scored concepts from prior sessions (re-study)
2. Overdue concepts (most days past due first)
3. Newly added concepts (never reviewed)

### 2. Present title + description only

For each concept in the queue:

```
📚 Review #N of ~M

**[Concept Title]**
> One-line description from frontmatter

What are the key ideas? Explain from memory.
```

**Do NOT show the concept body.** This is retrieval practice — the struggle is the point.

### 3. User recalls and self-rates

Let the user answer (verbally or typed). Then:

```
Rate your recall:
  ✅ — Full recall, all key points present
  🟡 — Partial, core idea intact but details missed
  ❌ — Failed, couldn't recall the core idea
```

### 4. Show body for comparison

After the user rates, display the concept body:

```
--- Answer ---
[concept body content]
--------------
```

Note gaps: what was missed, what was wrong, what was vague. Keep it brief — one or two sentences.

### 5. Update interval and timestamp

Based on the rating, update the concept's frontmatter:

| Rating | `review_interval` | `timestamp` |
|--------|-------------------|-------------|
| ✅ | Advance one step on ladder | today |
| 🟡 | Halve (round down to ladder step) | today |
| ❌ | Reset to 1 | **don't update** (keeps it due tomorrow) |

For ❌, also prompt: "Re-study this concept now? (y/n)" — if yes, recommend using [okf-study](/skills/okf-study/SKILL.md) Step 1–4 on this concept.

### 6. Next concept

Move to the next concept in the priority queue. Repeat until timer expires.

## INTERVAL Procedure

Check what's due without starting a full session.

```bash
# List all learning concepts with their review status
python3 tools/okf.py search "domain:learning" --limit 200
```

For each concept, compute `days_overdue = (today - timestamp) - review_interval`.

Report:
- **Overdue** (days_overdue > 0): list with overdue count
- **Due today** (days_overdue = 0)
- **Upcoming** (due in next 3 days)
- **Not yet reviewed** (no `review_interval` field)

## STATS Procedure

Report review statistics for learning concepts.

```bash
python3 tools/okf.py search "domain:learning" --limit 500
```

Count and report:

| Bucket | Criteria |
|--------|----------|
| Overdue | `today - timestamp > review_interval` |
| Due today | `today - timestamp = review_interval` |
| Learning | `review_interval` ≤ 3 |
| Consolidating | `review_interval` 7–14 |
| Maintained | `review_interval` ≥ 30 |
| New | no `review_interval` field |
| Dormant | `status: dormant` |

Display as a compact summary:

```
📊 Review Stats (N total concepts)
  🔴 Overdue: X    🟡 Due today: Y
  📗 Learning: A   📘 Consolidating: B   📕 Maintained: C
  ✨ New: D        💤 Dormant: E
```

## Session Timer

- Set a 15–20 minute timer at session start.
- When time is up, finish the current concept's rating, then stop.
- Report session summary:
  - Concepts reviewed (with scores)
  - Concepts still in queue
  - Suggested next session time (based on soonest overdue concept)

## Integration with okf-study

The [okf-study](/skills/okf-study/SKILL.md) skill Step 5 (Schedule Review) uses this skill's interval ladder and `review_interval`/`timestamp` fields:

- After a study session's recall step, **update `review_interval` and `timestamp`** per the ladder table in Step 5.
- A study session may include a mini-review of stale concepts before starting new material: run SESSION procedure for 5 minutes, then proceed with study.

## Methodology References

- [Spaced Repetition](/concepts/learning/methods/spaced-repetition.md) — the forgetting curve and optimal review timing
- [Retrieval Practice](/concepts/learning/methods/retrieval-practice.md) — why recalling from memory is more effective than re-reading
- [Deliberate Practice](/concepts/learning/methods/deliberate-practice.md) — targeting weaknesses rather than rehearsing strengths
- [Interleaving](/concepts/learning/methods/interleaving.md) — mixing topics across review sessions for deeper encoding
- [Elaborative Encoding](/concepts/learning/methods/elaborative-encoding.md) — connecting new knowledge to existing concepts
