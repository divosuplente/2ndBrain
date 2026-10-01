---
type: skill
name: okf-study
description: "Govern a self-study session for non-domain-specific topics using the OKF vault."
---

# OKF Study Skill

## Trigger Patterns
- "study <topic>"
- "I want to learn <topic>"
- "study session"
- "review my notes on <topic>"
- "let me work through <topic>"
- "/study <topic>"

## Pre-session Checklist

Before starting, the agent MUST:

1. **Identify the topic** — what domain/concept area? Check `concepts/learning/` for existing concepts.
2. **Check prerequisites** — is the user ready for this topic? Consult the prerequisite graph ([Study Prerequisite Graph](/concepts/learning/study-prerequisite-graph.md)) or roadmap concepts.
3. **Surface related concepts** — `python3 tools/okf.py search "<topic>"` to find what's already in the vault.
4. **Check stale concepts** — find concepts in this domain with `timestamp` older than 7 days. These are retrieval practice candidates.

## The Study Loop (5 steps)

### Step 1: Read / Encode

If studying from a source (textbook, video, paper):
- Use `okf-ingest` if the source isn't in the vault yet
- If already ingested, read the concept

If reviewing from memory:
- Go directly to Step 2

### Step 2: Recall (Retrieval Practice)

**Do NOT show the concept body yet.** Instead:

1. State the concept title
2. Ask: "Explain this in your own words. What are the key ideas? What equations/definitions are essential?"
3. Let the user respond (or if the user asks the agent to self-test, the agent attempts to summarize from the title alone)
4. Compare the recall attempt against the actual concept body
5. Note gaps — what was missed, what was wrong, what was vague

**Scoring** (self-rated by the user — drives spaced repetition intervals):
- ✅ Full recall — all key points present
- 🟡 Partial — some points missed but core idea intact
- ❌ Failed — couldn't recall core idea

Update: if ✅, concept `timestamp` → today. If 🟡 or ❌, leave `timestamp` unchanged (stays in review queue).

**Supplementary in-session quiz** (optional, does not affect intervals):
After self-rated recall, the agent may present a quick multiple-choice drill to reinforce retrieval. Construct options by writing the correct answer first, then mutating it into plausible distractors. No asymmetric bolding, no telegraphing. Reveal ✓/✗ with a brief explanation. "I don't know" tracked separately from wrong — signals need to re-encode before re-testing. This is practice, not assessment; interval scheduling always follows the self-rated ✅/🟡/❌ above.

### Step 3: Practice (Problem-Solving)

If the topic has associated problems:
1. Select a problem from the textbook or problem set
2. For interleaving: if this is the 3rd+ problem in a session, pick from a DIFFERENT sub-topic
3. Use Pólya's 4-step framework to guide:
   - Understand: can you restate the problem? What's given? What's asked?
   - Plan: what approach? What principles apply?
   - Execute: solve it
   - Look back: check units, order of magnitude, special cases
4. Record the problem using `okf-problem-journal` skill

### Step 4: Connect (Elaborative Encoding)

1. Read the concept body (if not already read)
2. Identify at least 2 related concepts — use `python3 tools/okf.py search` on key terms
3. Add cross-links to the concept if missing
4. Ask: "How does this connect to [related concept]? What's the relationship?"

### Step 5: Schedule Review

Use [okf-review](/skills/okf-review/SKILL.md) for spaced-repetition scheduling. Update both `review_interval` and `timestamp` in the concept's frontmatter using the interval ladder:

| Score | `review_interval` | `timestamp` |
|-------|-------------------|-------------|
| ✅ Full recall | Advance one step (1d→3d→7d→14d→30d) | today |
| 🟡 Partial | Halve current interval (round down to ladder step) | today |
| ❌ Failed | Reset to 1d | **don't update** (stays due tomorrow) |

If `review_interval` is absent, treat as new (start at 1d). If already at 30d and ✅, stay at 30d.

For a dedicated review session (not during study), use `okf-review` directly.

## Session Output

At end of session, report:
1. Concepts reviewed (with recall scores)
2. Problems solved (with difficulty ratings)
3. New cross-links added
4. Next session suggestion (what to study next, based on prerequisite graph)

## Companion Skills

Load these skills when the study session reaches the corresponding step. They are NOT loaded automatically — the agent must invoke them explicitly.

| Skill | When to load | Relationship |
|-------|-------------|--------------|
| **okf-ingest** | IF the source material is not yet in the vault (Step 1: Read/Encode) | Fetches external content |
| **okf-problem-journal** | IF recording a worked problem (Step 3: Practice) | Records problem with Pólya structure |
| **okf-review** | IF scheduling spaced repetition (Step 5: Schedule Review) | Manages review intervals |
| **okf-book-ingest** | IF converting a textbook chapter to a concept (Step 1: Read/Encode) | Extracts chapter content |
| **visualize** | IF the topic has structural, spatial, or relational ideas that would be clearer as a diagram | Generates diagrams

## Methodology References

- [Retrieval Practice](/concepts/learning/methods/retrieval-practice.md)
- [Spaced Repetition](/concepts/learning/methods/spaced-repetition.md)
- [Feynman Technique](/concepts/learning/methods/feynman-technique.md)
- [Deliberate Practice](/concepts/learning/methods/deliberate-practice.md)
- [Pólya Problem Solving](/concepts/learning/methods/polya-problem-solving.md)
- [Interleaving](/concepts/learning/methods/interleaving.md)
- [Elaborative Encoding](/concepts/learning/methods/elaborative-encoding.md)
