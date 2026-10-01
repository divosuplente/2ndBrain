---
type: skill
name: okf-ingest-channel
description: "Batch-fetch ALL transcripts from a YouTube channel and ingest them into the OKF brain."
---

# OKF Ingest Channel — Batch YouTube Channel Ingest

## Taxonomy reference (do not paste tables here)
- Shared map: `_config/taxonomy.md` (`@tax`)
- **FBC mandatory:** read FULL source body to choose domain/subdomain/subsubdomain and semantic tags.
- Map is extensible — if content does not fit, extend `@tax` + hubs (see extension protocol there).
- Tag *cleanup* (banned noise tags, singular/lowercase) is in `@tax`; do not keyword-assign tags.

## Full-body classification (non-negotiable)
1. Read the entire source body/transcript.
2. Place and tag from actual meaning — not filename, title keywords, or static enums alone.
3. Prefer known leaves in `_config/taxonomy.md` when they fit.
4. Otherwise extend the map per taxonomy extension protocol.


Fetch every transcript from a YouTube channel and ingest them into the OKF brain as concepts. This is a **batch** operation — unlike `okf-ingest` (one URL at a time), this skill processes an entire channel's catalog.

Uses yt-dlp for both channel enumeration and subtitle download. Free, no API key required.

## Universal Agent Compatibility

This skill has two execution paths depending on your agent's capabilities:

### Path A: OMP/eval-kernel agents
Use `completion()` + `parallel()` for extraction and page writing as described in Phases 2-4 below. Fastest: ~5 min for 14 videos.

### Path B: Universal agents (read/write/bash/delegate only)
If your agent lacks `completion()` or `parallel()`:

| Phase | OMP Path | Universal Path |
|-------|----------|----------------|
| 2 Extract | `completion()` + `parallel()` | Delegate to task subagents: one per video (or 2-3 videos per subagent). Give each subagent the extraction JSON schema and prompt inline. Subagents write their `raw/youtube/extractions/<slug>.json` directly. |
| 3 Canonicalize | `completion()` semantic dedup | Read all extraction JSONs yourself. Group by name similarity. For ambiguous cases, use your judgment or ask the user. Apply ≥2-video threshold manually. |
| 4 Write pages | `completion()` + `parallel()` | Delegate to task subagents: batch 5-10 pages per subagent. Give each subagent the concept/entity details, prompt template, and write path. Or write pages sequentially yourself if batch is small (<20). |
| 5-8 | Same | Same (deterministic checks run identically) |

**Universal subagent contract** (extraction):
```
Read raw/youtube/<slug>.md. Extract concepts and entities as JSON.
Write to raw/youtube/extractions/<slug>.json.
Schema: {"concepts": [{"name": "...", "description": "...", "quote": "..."}], "entities": [{"name": "...", "type": "tool|person|organization|product", "description": "...", "url": "..."}]}
Return ONLY the file path when done.
```

**Universal subagent contract** (page writing):
```
Write OKF concept page for <name>.
Description: <description>
Quote: <quote>
Source videos: <video urls>
Write to: concepts/<domain>/<subdomain>/<slug>.md
Include frontmatter (type, visibility, title, description, domain, tags, source, generated, status).
Include body with explanation, ## Related, ## Sources sections.
Return ONLY the file path when done.
```

**Warning**: Universal path is slower (serial or limited-parallel delegation) but produces identical output. Use `--since 1y` to limit channel size if batching is constrained.

## When to Use This Skill

- You want to ingest **all** (or many) videos from a YouTube channel
- You want to build a knowledge domain from a creator's entire catalog
- `okf-ingest` is for single URLs; this is for channels

## Prerequisites

- `yt-dlp` installed (`pip install yt-dlp` or `uv tool install yt-dlp`)
- Read [OKF Core](/concepts/skills/okf-core.md) for vault conventions
- Read [OKF Ingest](/concepts/skills/okf-ingest.md) for the per-video ingest workflow

---

## The 8-Phase Pipeline

This skill implements the [OKF Extraction Pipeline](/concepts/tools/okf-pipeline.md). Each phase below is what you (the agent) do. The validation scripts in `tools/validation/` are deterministic and run anytime.

### Phase 1: Source — Fetch All Transcripts

```bash
# Full channel
uv run skills/okf-ingest-channel/scripts/fetch_transcripts.py @ChannelHandle --output-dir ./raw/youtube

# Only videos from the last year (recommended for large channels)
uv run skills/okf-ingest-channel/scripts/fetch_transcripts.py @ChannelHandle --since 1y --output-dir ./raw/youtube

# Test run (5 videos)
uv run skills/okf-ingest-channel/scripts/fetch_transcripts.py @ChannelHandle --limit 5 --output-dir ./raw/youtube
```

Enumerates all videos, downloads English subtitles as json3, converts to timestamped OKF raw-transcript pages (`type: raw-transcript`, `immutable: true`). Output: `raw/youtube/<slug>.md` + `raw/youtube/manifest.json`.

Flags:
- `--limit N` — max videos to fetch (start with 5 for testing)
- `--since 1y` — only fetch videos from the last year. Accepts relative (`1y`, `2y`, `365d`, `30d`) or absolute (`2025-01-01`, `20250101`) dates. Older videos are NOT fetched but listed in `manifest.json` under `older_videos` (with slug, title, URL, published date) so you can cherry-pick later.
- `--delay 3.0` — seconds between requests (increase if hitting 429s)

Wait for the script to complete before proceeding.

### Phase 2: Extract (per video, batch in groups of 10)

**CRITICAL: Use `completion()` from the eval kernel, NOT subagents.** Subagents on smaller models fail at JSON extraction and page writing (parse errors, skill-loading loops, sandbox paths). The session model via `completion()` works reliably.

```python
# Extract one video:
result = completion(EXTRACTION_PROMPT.format(title=title, url=url, transcript=body), model="default")
# Parse JSON, strip markdown fences, write to raw/youtube/extractions/<slug>.json
#
# Batch 10 videos in parallel:
results = parallel([lambda v=v: extract_one(v) for v in batch])
```

Extraction JSON schema (compact, proven):

```json
{
  "concepts": [
    {"name": "Display Name", "description": "1-2 sentence explanation", "quote": "verbatim quote with timestamp"}
  ],
  "entities": [
    {"name": "Proper Name", "type": "tool|person|organization|product", "description": "what it is", "url": "canonical URL or null"}
  ]
}
```

Rules:
- Write to `raw/youtube/extractions/<slug>.json` (NOT `tools/extractions/`)
- Process in parallel batches of 10 via `parallel()`
- Strip markdown code fences from `completion()` output before JSON parse
- Retry with stricter instruction if initial parse fails
- Only extract what the video ACTUALLY teaches substantively (typically 5-20 concepts, 3-15 entities)
- Use STABLE, GENERAL slugs: `the-piv-loop` not `plan-implement-validate-loop`

- Quotes must be VERBATIM from the transcript at real timestamps
- Skip sponsor reads, one-off tool mentions, and noise

### Phase 3: Canonicalize (once, serial — the barrier)

**This is the critical step.** After ALL extractions are complete, merge them into a single frozen taxonomy. Do NOT skip this — it is what prevents duplicate pages with divergent content.

1. **Aggregate** — load all `raw/youtube/extractions/*.json`. Group concept candidates by normalized name (lowercase). Count how many distinct videos mention each.
2. **Semantic dedup via `completion()`** — send ALL concept name+description pairs to a single `completion()` call asking for merge groups (indices that express the same idea). Do the same for entities. Example prompt:
   ```
   Group concept indices that express the same or similar idea. Return JSON with concept_merge_groups: [{indices: [i,j], canonical: k}]
   0: Concept A — description...
   1: Concept B — description...
   ```
3. **Apply merges** — for each merge group, keep the canonical entry; discard variants.
4. **Apply page-creation threshold:**
   - A concept gets its own page if it appears in **≥2 videos** OR is the subject of one clearly substantial deep-dive
   - Below that bar: a mention on a parent page, not its own file
5. **Save final lists** — write `raw/youtube/extractions/_final_concepts.json` and `_final_entities.json` with canonical name, description, quote, videos list.

### Phase 4: Write OKF Pages (parallel batches of 10 via `completion()`)

**CRITICAL: Use `completion()` from the eval kernel, NOT subagents.** Same failure modes as Phase 2.

```python
# Define CONCEPT_PAGE_PROMPT and ENTITY_PAGE_PROMPT templates
# Define write_concept_page(concept) and write_entity_page(entity) functions
# Each calls completion(), strips fences, builds frontmatter, writes file
#
# Process in parallel batches of 10:
results = parallel([lambda c=c: write_concept_page(c) for c in batch])
```

Page structure:
1. Build frontmatter: `type`, `visibility`, `title`, `description`, `domain`, `tags`, `source` (video URLs), `generated`, `status`
2. Call `completion()` with concept details, quote, source video links → returns body text
3. Strip markdown code fences from output
4. Concatenate frontmatter + body → write to `concepts/<domain>/<subdomain>/<slug>.md`
5. Entity routing: `type: tool`/`product` → `concepts/tools/`; `type: person` → `concepts/people/`; `type: organization` → `concepts/orgs/`
6. Concept routing: use domain/subdomain from extraction or classify by content

After writing ALL pages:
1. **Check for thin/empty bodies** — scan all new pages for <100 chars (concepts) or <50 chars (entities)
2. **Re-generate thin pages** with a tighter prompt that includes the full page template inline
3. **Verify ## Sources exists** on every page — add missing ones programmatically
4. **Wire cross-links** — add `## Related` sections with typed subheadings (`## Tools`, `## Related`) and one-line descriptions to each concept page linking to its source videos and related concepts
5. **Create creator concept** — write `concepts/creators/general/<channel-slug>.md` with channel bio, video count, and links to all extracted concepts. Wire bidirectional links: concept pages ↔ creator page.
#### Domain Routing Rules

Classification based on content, not keywords:
- **`life`** = ONLY neurodivergent, personal, travel, mindfulness
- **`learning`** = cooking, health, keto, aromatherapy, dev, languages, music, sharepoint, and general knowledge
- **`tools`** = agents, dev, general. NO `tools/health`. Health devices → `tools/dev/devices/`
- Routing: `tools/dev/coding-agents` → `tools/agents/coding-agents`; `tools/dev/orchestration` → `tools/agents/orchestration`
Valid subdomains:
- `creators`: [general]
- `documents`: [general]
- `specs`: [general]
- `orgs`: [general]
- `people`: [medical, tech, general]
- `skills`: [claude-code, content, general]
- `work`: [sharepoint, azure, general]
- `life`: [neurodivergent, personal, travel, mindfulness]
- `tools`: [agents, dev, general]
- `learning`: [dev, languages, music, skills, keto, cooking, health, sharepoint, aromatherapy, general]

Valid subsubdomains:
- `learning/dev`: [javascript, react, css, typescript, vue, svelte, html, git, dotnet, api, performance, ai, architecture]
- `learning/health`: [fitness, nutrition]
- `learning/languages`: [japanese, korean, chinese, german, dutch]
- `learning/music`: [vocal-technique]
- `learning/skills`: [journaling]
- `tools/agents`: [orchestration, coding-agents, sandboxes, memory, mcp, general]
- `tools/dev`: [general, devices]
- `tools/general`: [devices, general, orchestration]

### Phase 5: Polish

- **ASR normalization** — YouTube auto-captions garble proper nouns. Sweep pages for known transcription errors: "Enthropic"→"Anthropic", "pantic AI"→"Pydantic AI", "Lang graph"→"LangGraph", "canban"→"Kanban". Fix ONLY proper nouns and technical terms. Preserve timestamps, meaning, and all other wording exactly.
- **Typed relationships** — ensure `## Related` sections use typed subheadings with one-line descriptions rather than bare link lists.

### Phase 6: Validate

Run all deterministic checks:

```bash
python3 tools/okf.py index                              # rebuild search index
python3 tools/okf.py lint                               # OKF conformance, links, orphans
python3 tools/okf.py relink --dry-run                   # check for link rewrites
python3 tools/okf.py relink                             # fix links (only if dry-run shows rewrites)

python3 tools/validation/audit.py                       # frontmatter, thin pages, dup titles
python3 tools/validation/citation_check.py               # quote integrity vs raw transcripts
python3 tools/validation/sweep.py                        # leakage, encoding, link-graph health
```

**Tag validation** — for each written page, verify:
- No `youtube` or `clippings` tags (stale ingest artifacts)
- No domain-redundant tags (tag value same as domain)
- No uppercase, underscore, or garbage tags (must be lowercase-hyphen)
- At least 1 meaningful tag remains after cleanup

Fix every error. Update `index.md` counts. Append `log.md` entry.

### Phase 7: Gap Sweep

Detect and fill coverage gaps — durable ideas that no page covers yet.

1. **Detect** — for each video's extraction JSON, check which mined concepts/entities have no page. Match by MEANING, not exact string.
2. **Notability triage** — for single-video candidates, score KEEP/DROP:
   - **KEEP**: widely-known product/model/company, central to the video, durable infrastructure that recurs in the ecosystem
   - **DROP**: sponsor reads, obscure micro-tools, one-off demo dependencies, things nobody will ask about
   - Be discriminating: a knowledge base is degraded by thin pages about things nobody will ask about
3. **Fill** — write the approved new pages with cited quotes and reciprocal backlinks. Apply the same full-body classification, tag audit, cross-link wiring, and creator-page linking as Phase 4.

### Phase 8: QA

Two LLM checks (you do these as the final validation):

- **Answerability** — answer 5-10 real questions about the channel's content by navigating only the concept pages. Verify each answer cites real concepts. Then try 3-5 trap questions (out-of-scope/false premise) — the correct behavior is to say "not covered," NOT to invent an answer.
- **Recall coverage** — sample 5-10 videos, extract their core ideas, verify each durable idea has a covering concept page. Target: >90% coverage.
- **Enroll channel for tracking** — register the channel in `tools/channel_check.db` so periodic checks detect new videos:

```bash
sqlite3 tools/channel_check.db "INSERT OR IGNORE INTO channels (handle, url, label) VALUES ('@ChannelHandle', 'https://www.youtube.com/@ChannelHandle/videos', 'Channel Label')"
```

Extract `@ChannelHandle` and `Channel Label` from the manifest or creator page. `INSERT OR IGNORE` makes this idempotent.
---

## Batching for Large Channels

For channels with 50+ videos:
- **Extract:** 10 videos per batch via `parallel()` + `completion()`
- **Canonicalize:** do ONCE after all extractions complete; use `completion()` for semantic dedup
- **Write:** 10 pages per batch via `parallel()` + `completion()`
- **Gap sweep:** run after all pages are written
- Save `raw/youtube/extractions/*.json` between sessions
- **Do NOT run `okf.py relink` or `okf.py lint` during parallel processing** — run once at the end

## Output Structure

```
raw/youtube/
  <video-slug>.md            # OKF raw-transcript pages (immutable)
  manifest.json              # Fetch manifest with video metadata
  extractions/               # Phase 2 output (JSON per video)
    <video-slug>.json
    _final_concepts.json     # Phase 3 canonical concepts
    _final_entities.json     # Phase 3 canonical entities
tools/
  validation/                # Deterministic validation scripts
    audit.py
    citation_check.py
    sweep.py
concepts/tools/agents/       # Concept pages
  <slug>.md
concepts/tools/              # Entity pages (tools/products)
  <slug>.md
concepts/people/             # Entity pages (people)
  <slug>.md
concepts/orgs/               # Entity pages (organizations)
  <slug>.md
concepts/creators/           # Creator concept page (channel bio + video links)
  general/
    <channel-slug>.md
```

## Comparison with okf-ingest

| Aspect | okf-ingest | okf-ingest-channel |
|--------|-----------|-------------------|
| Scope | One URL/video at a time | Entire YouTube channel |
| Transcript fetch | Per-video yt-dlp | Batch channel enumeration + fetch |
| Ingest workflow | Full (creator → video → tools → cross-link) | 8-phase pipeline (extract → canonicalize → write → polish → validate → gap sweep → QA) |
| Classification | Full-body per concept | Full-body per concept (mandatory, with subsubdomains) |
| Canonicalization | None needed (single video) | Cross-video semantic dedup via `completion()` (or agent judgment). Group by meaning. Apply ≥2-video threshold. |
| Theme reconciliation | Per-concept | Batch reconciliation via extraction metadata |
| Hub backlinks | Depth-conditional | Depth-conditional with subsubdomain support |
| Best for | Individual videos/URLs | Building a knowledge domain from a creator's catalog |


## `ingest.py` Caveats

`tools/ingest.py` has known limitations requiring a **mandatory post-ingest cleanup pass**:

1. **Slug-from-path bug**: When `parse_source()` fails to extract a title, `slugify()` falls back to `source_ref` (file path), producing garbage slugs (`users-ima-okf-inbox-...`). Always verify and rename slugs post-ingest.
2. **No subdomain support**: `ingest.py` only accepts `--domain`. Subdomain routing must be done post-ingest via file moves.
3. **Title extraction may fail**: Obsidian clipping frontmatter produces mangled titles. Inspect all frontmatter after ingest.
4. **Post-ingest cleanup is mandatory**: Rename garbage slugs → move to correct subdomain/subsubdomain paths → fix frontmatter → delete true duplicates (check existing file quality for merge).
5. **Title extraction from headings**: Skip structural headings (`Transcript`, `Features`, `Installation`, `Readme`, `Overview`, `Introduction`) — not suitable concept titles.

## ICM stages
Stepwise contracts live under `skills/okf-ingest-channel/stages/` (01 fetch → 08 QA). Load only the current stage `CONTEXT.md`.
