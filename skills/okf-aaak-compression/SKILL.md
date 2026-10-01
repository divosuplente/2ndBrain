---
type: skill
name: okf-aaak-compression
description: "Compress agent-only skill files using MemPalace AAAK dialect syntax."
---

# OKF AAAK Compression — Dual-Layer Skill Compression

## When to Use
- User says "compress skills", "apply AAAK", "optimize skill tokens"
- Editing skill files under `skills/` for token efficiency
- Rewriting or creating new skills that agents will load

## What It Does
Creates a dual-layer system per skill:
- **SKILL.md** — compressed agent view using AAAK syntax (what agents load)
- **SKILL.full.md** — verbatim original (source of truth, human-readable)

## Process

### Step 1: Preserve Verbatim Source
Copy the original `SKILL.md` to `SKILL.full.md` before any modification. This is the lossless backup.

### Step 2: Write Compressed SKILL.md

Structure:
```
---yaml frontmatter (COPIED VERBATIM from SKILL.full.md)---

FMT:AAAK-OKF-v2|lossy-agent-overlay|verbatim-source=SKILL.full.md
DICT:cx=concepts|fm=frontmatter|vis=visibility|dom=domain|sub=subdomain|YT=YouTube|tsx=transcript|xl=cross-link|hub=hub-file|raw=raw-snapshot|slug=kebab-case-id|!ex=if-not-exists→create|⊕=create-write|⊖=delete-remove|∀=for-each|→=then-leads-to|@core=see-okf-core|@ing=see-okf-ingest

SECTION_PREFIX:pipe-separated|hyphenated-concepts|article-dropped
```

### Step 3: Apply AAAK Transformations

Based on MemPalace's `dialect.py` and `AAAK_SPEC` (from github.com/MemPalace/mempalace):

| Transform | Example | Rule |
|-----------|---------|------|
| Section prefixes | `## Visibility Rules` → `VIS:` | Replace markdown headings with short prefix + colon |
| Pipe separation | `private. shareable.` → `private|shareable` | Pipes between concepts |
| Hyphenated words | `front matter` → `front-matter` | Hyphens within concepts |
| Article dropping | `the domain hub` → `domain-hub` | Drop the/a/an — no semantic loss |
| Abbreviation glossary | `concepts` → `cx` | DICT line defines all abbreviations |
| Cross-references | `see okf-core` → `@core` | `@` prefix for skill refs |
| Symbolic operators | `if not exists, create` → `!ex→create` | `⊕` create, `⊖` delete, `∀` for-each, `→` then |

### Step 4: Preserve These Verbatim (NEVER compress)

1. **YAML frontmatter** — copy byte-for-byte from SKILL.full.md (routing depends on it)
2. **YAML examples** inside ` ```yaml ` blocks — must be valid multiline YAML
3. **Bash blocks** inside ` ```bash ` blocks — must be copy-pasteable commands
4. **JSON blocks** — preserve structure exactly
5. **Write instructions** — use canonical key names (`visibility`, not `vis`)
6. **`vis` shorthand** — only appears in DICT glossary line, never in write instructions

### Step 5: Verify

After writing the compressed file:
1. Frontmatter `name` and `description` in SKILL.md MUST match SKILL.full.md exactly
2. No `viz` anywhere (use `vis` in DICT)
3. All YAML blocks valid multiline (no comma-packed keys)
4. FMT line declares `lossy-agent-overlay` and `verbatim-source=SKILL.full.md`
5. All semantic content preserved (every instruction, constraint, threshold, path, rule)

## Design Constraints

- **Not lossless**: The compressed SKILL.md is a lossy agent overlay. The verbatim SKILL.full.md preserves the original. This mirrors MemPalace's architecture: drawers (verbatim) + closets (AAAK compressed index).
- **Agent-native**: Agents read SKILL.md directly — no decoder, no external tooling. The DICT line makes abbreviations self-documenting.
- **Routing-safe**: Skill discovery depends on frontmatter `name` and `description` fields, which are copied verbatim.

## Glossary of Standard Abbreviations

```
cx     = concepts/          vis    = visibility
fm     = frontmatter        YT     = YouTube
tsx    = transcript          hub    = hub file
xl     = cross-link          raw    = raw/ snapshot
dom    = domain              sub    = subdomain
slug   = kebab-case-id       !ex    = if not exists → create
⊕     = create/write         ⊖     = delete/remove
∀     = for all / for each   →     = then / leading to
@core  = see okf-core         @ing  = see okf-ingest
```

Skills may extend the glossary with domain-specific abbreviations. Every abbreviation used in the body MUST appear in the DICT line.

## Applied To

- `skills/okf-core/` — SKILL.md (compressed) + SKILL.full.md (verbatim)
- `skills/okf-ingest/` — SKILL.md (compressed) + SKILL.full.md (verbatim)
- `skills/okf-ingest-channel/` — SKILL.md (compressed) + SKILL.full.md (verbatim)
- `skills/okf-batch-ingest/` — SKILL.md (compressed) + SKILL.full.md (verbatim)
