---
type: skill
name: okf-journal
description: "Ingest journal/therapy notes into OKF brain. Always visibility private, never shareable export."
---

# OKF Journal — Private personal & therapy ingest

## Trigger patterns
- "journal", "therapy", "session notes", "journaling", "reflection", "log this session"
- Personal/sensitive content to save (mental health, recovery, private life events)

## Non-negotiable rules
- **ALWAYS** `visibility: private`
- **ALWAYS** personal-domain placement under `life/` (never `shareable`)
- **FBC:** read full entry body to choose subdomain (`personal`, `neurodivergent`, `mindfulness`, `travel`, …) and tags — see `_config/taxonomy.md` (extensible)
- **NEVER** mark journal entries `shareable` or include them in exports
- Prefer `skills/okf-journal` over generic ingest for this content (ingest defaults are often shareable)

## Type mapping
| Content | `type` |
|---------|--------|
| Therapy / session notes | `note` |
| Habit tracking | `habit` |
| Goals / intentions | `goal` |
| Topic reflections | `topic` |
| Life event docs | `note` |

## Path
`concepts/life/<subdomain>/journal-<YYYY-MM-DD>-<topic-slug>.md`

Examples:
- `concepts/life/personal/journal-2026-08-06-weekly-reflection.md`
- `concepts/life/neurodivergent/journal-2026-08-06-therapy-session.md`

## Frontmatter template
```yaml
---
type: note
visibility: private
title: <descriptive title>
description: <one-line summary>
domain: life
tags: [journal, therapy, <topic-tags-from-FBC>]
timestamp: <session date or today ISO>
status: active
source:
  - self:
---
```

## Body template
```markdown
# <Title>

## Session date
<YYYY-MM-DD>

## Summary
<1–2 sentences>

## Notes
<structured notes>

## Insights
<takeaways>

## Action items
- <next steps>

## Related
- [parent hub]
- [therapy / recovery concepts when relevant]
```

## Cross-link targets (resolve current paths)
Search first (`okf search`); paths may live under `life/neurodivergent/` after subdomain migration:
- therapy, binge-eating recovery, journaling-binge-eating-therapy, urge-surfing, ADHD/autism notes as applicable
- Always depth-conditional backlink to parent hub (`life/personal`, `life/neurodivergent`, …)

## Pipeline after write
```bash
python3 tools/okf.py index
python3 tools/okf.py lint
python3 tools/okf.py relink --dry-run
# append log.md with journal summary — never echo private body in shareable contexts
```

## Privacy guarantee
- Filtered out of any shareable export
- When unsure → `private`
- Do not paste full journal bodies into public docs, PRs, or shareable concepts

## Companion Skills

| Skill | When to load | Relationship |
|-------|-------------|--------------|
| **okf-therapy** | IF journal content involves therapy, psychology, psychiatry, or clinical mental health topics | Adds clinically-aware tags, recognizes therapy frameworks, reinforces privacy |
