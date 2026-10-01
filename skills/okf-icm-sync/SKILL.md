---
type: skill
name: okf-icm-sync
description: "Sync OKF ICM orientation files (IDENTITY.md, CONTEXT.md) with the live vault."
---

# okf-icm-sync

Keep Layer 0/1 accurate without restructuring the corpus.

## When to run
- New skill under `skills/`
- New top-level pipeline or `_config` slice
- User reports wrong routing / missing skill in CONTEXT
- After large vault layout changes (not every single concept ingest)

## Inputs
- Disk: `skills/*/`, `IDENTITY.md`, `CONTEXT.md`, `index.md`, `rules/`, `_config/`
- Contract: `AGENTS.md` (for golden rules drift only)

## Steps
1. List invocable skills: directories under `skills/` that contain `SKILL.md` (skip `_reorg` unless it gains a skill entrypoint).
2. Diff against routing rows in `CONTEXT.md` — add/update/remove task rows.
3. Diff map table in `IDENTITY.md` against real top-level dirs (do not invent folders).
4. Ensure adapter stubs still point at ICM load order:
   - Prefer: IDENTITY → CONTEXT → skill/stage
   - `AGENTS.md` remains deep contract, not first 50k dump
5. If domain list or personal-domain set changed in AGENTS, mirror into `_config/glossary.md` / `conventions.md`.
6. Append `log.md` only when you changed ICM files:

```markdown
## [YYYY-MM-DD] icm-sync | <what changed>
```

## Outputs
- Updated `IDENTITY.md` and/or `CONTEXT.md` (and `_config/*` if needed)
- No concept body rewrites unless fixing a link to renamed ICM paths

## Guards
- Do **not** renumber `concepts/`
- Do **not** duplicate full AGENTS into IDENTITY
- Token budgets: IDENTITY ≲ 800–1200 tokens; CONTEXT ≲ 500–800 tokens
- Path access: stay inside vault allow-list (`rules/path-access-control.md`)

## Done when
- Every invocable skill is reachable from CONTEXT routing or explicitly internal
- IDENTITY map paths exist on disk
- Adapters mention ICM entry (IDENTITY/CONTEXT)

## Taxonomy
When domains/subs change durably, update `_config/taxonomy.md` (extensible reference) as part of sync.
