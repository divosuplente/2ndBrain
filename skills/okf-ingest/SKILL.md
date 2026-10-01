---
type: skill
name: okf-ingest
description: "Ingest a single URL, YouTube video, or web article into the OKF brain."
---

# OKF Ingest — Universal Content Ingest Workflow

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


## STAGES Pipeline

Stage files live in `stages/` — load `CONTEXT.md` for each step when running stepwise:

| Stage | Name | Purpose |
|-------|------|---------|
| 00 | [Preflight](stages/00-preflight/CONTEXT.md) | Answer mandatory clarifying questions before touching any files |
| 01 | [Snapshot](stages/01-snapshot/CONTEXT.md) | Capture source material verbatim on disk |
| 02 | [Concept](stages/02-concept/CONTEXT.md) | Compile raw source into canonical concept(s) |
| 03 | [Cleanup](stages/03-cleanup/CONTEXT.md) | Fix ingest debt (slugs, subdomains, duplicates) |
| 04 | [Relink · Index · Log](stages/04-relink-index/CONTEXT.md) | Make corpus mechanically consistent, record the run |

Flow: `00-preflight → 01-snapshot → 02-concept → 03-cleanup → 04-relink-index`


## Stage 00 — Preflight (mandatory first step)

Before fetching, snapshotting, or creating any concept, answer these six questions. Prevents misplacement, privacy leaks, and duplicate creation.

### 1. Domain placement
Based on FBC of the full body, which domain/subdomain/subsubdomain?
- Reference `_config/taxonomy.md` for known leaves and extensible paths
- If content is not yet fetched (URL), note the intended domain and confirm after fetch
- Never force-fit; extend taxonomy if genuinely no leaf fits

### 2. Visibility
Should this be `private` or `shareable`?
- Default by domain: `life`, `people`, `orgs`, `documents`, `work` → `private`; others → `shareable`
- Override only with explicit justification (e.g., `#private` hashtag, user instruction, sensitive content in a shareable domain)
- When unsure → `private`

### 3. Type
Which OKF unified type? (`key-element`, `goal`, `habit`, `project`, `topic`, `person`, `organization`, `document`, `tool`, `spec`, `skill`, `learning`, `source`, `playbook`, `reference`, `note`)
- Fine grouping is carried by `domain` + `tags`, not by inventing new types
- `note` is the fallback when nothing else fits

### 4. Existing conflict
Does a concept with similar title/slug already exist?
- Search: `okf search "<proposed title>"` and check `index.md`
- If conflict found, decide: **skip** (existing is sufficient), **overwrite** (replace inferior), or **rename-with-suffix** (both deserve to exist)
- Record the decision and the conflicting concept path

### 5. Tag handling
Source has tags — flatten into `tags:` array or reshape recurring tags into Topic concepts?
- Default: flatten into `tags:` array with cleanup (lowercase, hyphenated, singular, remove `clippings` and domain-redundant tags)
- If a tag recurs across ≥3 concepts and represents a substantive topic, consider promoting to its own concept
- User `#hashtags` merge with auto-tags; user tags win on conflict

### 6. Cross-link targets
Which existing concepts should this link to?
- Search for overlapping topics, shared tags, same domain, same creator, referenced tools
- Aim for ≥1 cross-link to existing concepts when possible
- Note target concept paths for downstream steps to include in body

Preflight answers carry forward to all subsequent stages. If using stepwise execution, load `stages/00-preflight/CONTEXT.md` first.


## Trigger Patterns
- User pastes a URL (with or without `#hashtags`)
- User says "ingest", "capture", "save this", "add to brain", "process this"
- User provides a YouTube link
- User provides an article URL to read and save

## Hashtag Parsing
When the user includes `#tag` tokens in their message, extract them as concept tags:
- `https://example.com/article #ai #ethics #toimplement` → `tags: [ai, ethics, toimplement]`
- Tags merge with auto-generated tags; user tags take precedence on conflict
- Common tags: `#toimplement`, `#review`, `#harness`, `#private`, `#journal`

## Full-Body Classification (FBC)

**Critical process change:** When determining where to write a concept, do NOT rely on keyword matching, tag tables, or filename hints. Instead:

1. **Read the ENTIRE body** of the source document (transcript, article body, etc.)
2. Based on what the content is **ACTUALLY about**, determine the domain/subdomain/subsubdomain per `@core-DOM` and `@core-SUB`
3. Use the `VALID_SUBDOMAINS` and `VALID_SUBSUBS` tables below as a reference for what folders exist — NOT as keyword match tables
4. If creating a `learning/dev` concept, check `VALID_SUBSUBS.learning.dev` for the correct subsubdomain
5. If creating a `tools/agents` concept, check `VALID_SUBSUBS.tools.agents` for the correct subsubdomain
6. If no subsubdomain fits, write to the subdomain root

### Domain Routing Changes
These reflect the updated routing (different from earlier versions of this skill):
- `life/` — **ONLY** neurodivergent, personal, travel, mindfulness
- `learning/` — everything else that isn't tools/skills/specs, including cooking, health, keto, aromatherapy
- `tools/agents` — agent frameworks (not `tools/dev`)
- `tools/dev/devices` — physical devices
- `learning/health` — health apps/content (not `tools/health`)
- `learning/aromatherapy` — NEW subdomain for aromatherapy content

## YouTube Video Ingest (special handling)

The YouTube pipeline has **two key innovations** beyond simple transcript-to-concept:
1. **VTT preprocessing** — raw auto-captions are noisy (overlapping cues, `<c>` timing tags, duplicate lines). A Python pass strips tags, dedupes cues, and produces clean prose before reading.
2. **Concept + Reference split** — the concept file is a concise overview; a `references/` sibling holds the deep knowledge extract with expanded primary sources (Wikipedia, original papers, citations).

### Step 1: Fetch Metadata + Transcript + Preprocess VTT
```bash
yt-dlp --dump-json --write-auto-sub --skip-download --sub-langs en --sub-format vtt -o "/tmp/okf_yt_<videoid>" "URL"
```
Extract from JSON: title, channel, uploader, upload_date, duration, view_count, description, tags.

**VTT preprocessing** (mandatory — never read raw VTT directly):
Raw VTT files have `<c>` timing tags, overlapping cues, and duplicate lines. A 5-hour video can produce 74K+ lines of noisy markup. Always preprocess before reading:

```python
import re, html

def preprocess_vtt(vtt_path: str, output_path: str) -> None:
    """Strip VTT markup, deduplicate cues, produce clean prose."""
    with open(vtt_path, 'r') as f:
        lines = f.readlines()

    seen = set()
    clean_lines = []
    in_cue = False

    for line in lines:
        line = line.strip()
        # Skip VTT headers, timing lines, cue identifiers
        if not line or line.startswith('WEBVTT') or line.startswith('NOTE'):
            continue
        if re.match(r'^[\d:.\-]+ --> [\d:.\-]+', line):  # timing line
            in_cue = True
            continue
        if in_cue:
            # Strip HTML tags (<c>, </c>, etc.)
            text = re.sub(r'<[^>]+>', '', line)
            text = html.unescape(text).strip()
            if text and text not in seen:
                seen.add(text)
                clean_lines.append(text)

    # Join into paragraphs (cue boundaries = natural sentence breaks)
    with open(output_path, 'w') as f:
        f.write('\n'.join(clean_lines))
```

For long videos (>30 min), split the preprocessed transcript into ~800-word chunks for systematic reading. Read ALL chunks — never sample.

### Step 2: Creator Concept
Check if the channel/creator already has a concept:
```bash
okf search "<channel name>"
```
- If exists → link the video concept to it
- If new → create `concepts/creators/<slug>.md`:
  ```yaml
  ---
  type: person
  visibility: shareable
  title: <Channel Name>
  description: <YouTube creator — <channel URL>>
  domain: creators
  tags: [<topic area>]
  timestamp: <today>
  status: active
  ---
  ```
  Body: channel description, topics they cover, channel URL, `## Videos` section listing ingested videos from this creator.
- All future videos from the same channel link to this creator concept
- Add the new video to the creator's `## Videos` section

### Step 3: Extract Linked Tools/Resources (description-first)
Scan the video description for ALL URLs. Common patterns:
- Tool/product websites
- GitHub repos
- Course/platform links
- Free resources (PDFs, newsletters, dictionaries)
- Related playlist links

For each linked tool/resource (skip social media profiles — they're not tools):
1. Fetch the linked page to understand what it is
2. **Apply FBC** to determine the correct domain/subdomain/subsubdomain:
   - Agent frameworks → `concepts/tools/agents/<ssub>/<slug>.md`
   - Physical devices → `concepts/tools/dev/devices/<slug>.md`
   - Health apps/content → `concepts/learning/health/<slug>.md`
   - Courses → `concepts/learning/<sub>/<slug>.md`
3. Frontmatter: `type: tool`, `visibility: shareable`, proper tags
4. Body includes `## Discovered In` cross-linking back to the video concept

**Transcript URL extraction:** Best-effort only. If a speaker clearly names a domain or URL verbally (e.g., "go to sjn-fumi.link"), capture it. Do NOT fabricate URLs that aren't clearly stated.

**Skip:**
- Social media profiles (Instagram, TikTok, Twitter handles) — not tools
- Generic YouTube playlist links — not tools
- Links to other videos on the same channel — not tools

### Step 4: Raw Snapshot (VTT only)
Write the **verbatim VTT file** to `raw/youtube/<video_id>.vtt`:
```bash
cp /tmp/okf_yt_<videoid>.en.vtt raw/youtube/<video_id>.vtt
```
Do NOT write a plain-text transcript `.md` to `raw/` — the preprocessed transcript is an intermediate working file, not a source artifact. The VTT is the authoritative verbatim snapshot; the knowledge extract lives in `concepts/.../references/` (Step 6).

### Step 5: Create Video Concept (overview)
**Apply FBC:** Read the FULL preprocessed transcript to determine what the content is ACTUALLY about. Based on the full body, classify into the correct domain/subdomain/subsubdomain per `@core-DOM` and `@core-SUB`. Do NOT rely on keyword matching or tag tables.

Write to `concepts/<dom>/<sub>/<ssub>/<slug>.md` or `concepts/<dom>/<sub>/<slug>.md` or `concepts/<dom>/<slug>.md` depending on classification depth:

```yaml
---
type: learning
visibility: shareable
title: <video title>
description: <one-line summary of the ACTUAL content, not just the title>
domain: learning
tags: [<parsed hashtags + auto tags>]
source:
  - https://www.youtube.com/watch?v=<video_id>
generated:
  by: agent:harness
  at: <ISO timestamp>
status: active
---
```

**Concept body = concise overview** (~50-80 lines). Structure:
- `# Title`
- One-paragraph summary: channel, duration, main thesis (from ACTUAL transcript, not description)
- Link to the deep reference file:
  ```markdown
  > **Full knowledge extract with expanded primary sources:** [Title — Knowledge Reference](/concepts/<dom>/<sub>/references/<slug>.md)
  ```
- Structured outline of the content (sections with one-line summaries, not full extracts)
- `## Related Concepts` — cross-link to existing OKF concepts
- `## Citations` — original video URL + key references

Search existing OKF concepts for related topics → add cross-links.
Add back-links from related concepts to the new video concept.

### Step 5b: Tag Audit
After creating the concept, review all tags on the new concept:
- **Remove** inappropriate tags: `youtube`, `clippings`, domain-redundant tags (e.g., `tools` tag on a `domain: tools` concept)
- **Ensure** all tags are lowercase-hyphen format
- **Add** missing meaningful tags derived from the full body content
- **Normalize** near-duplicate tags per `@core-TAGC` (e.g., merge singular/plural, collapse hyphen/underscore variants)
- **Ensure** at least one meaningful tag is present

### Step 5c: Depth-Conditional Backlink
Every new concept must include a backlink to its parent hub at the correct depth:
- If concept is in `concepts/<dom>/<sub>/<ssub>/<slug>.md` → backlink to `concepts/<dom>/<sub>/<ssub>.md`
- If concept is in `concepts/<dom>/<sub>/<slug>.md` → backlink to `concepts/<dom>/<sub>.md`
- If concept is in `concepts/<dom>/<slug>.md` → backlink to `concepts/<dom>/<dom>.md`

### Step 5d: Hub Update
After creating any concept, update its parent hub file at the correct depth (matching the backlink rules above):
1. Open the hub file at the depth determined by Step 5c
2. Add a link to the new concept in the appropriate tag section
3. Hub link labels must use `clean_title()` — strip markdown images/links/bold/italic/brackets/parens from frontmatter titles before using as link text
4. The domain hub auto-covers the new concept via its link to the subdomain hub — no update needed unless a new subdomain is created

### Step 6: Create Deep Reference File (knowledge extraction with sources)

**This is the core knowledge extraction step.** The reference file is NOT a transcript — it's a structured knowledge extract that expands on every concept mentioned in the source with grounded primary sources.

**Folder pattern:** Create a `references/` subfolder within the concept's domain/subdomain:
```
concepts/<dom>/<sub>/references/<slug>.md
```
Or for subsubdomain:
```
concepts/<dom>/<sub>/<ssub>/references/<slug>.md
```

**Reference file frontmatter** (same schema as concept, different title/description):
```yaml
---
type: learning
visibility: shareable
title: "<video title> — Knowledge Reference"
description: "Deep knowledge extract with expanded primary sources from <creator>'s <duration> <content type>."
domain: <same domain as concept>
tags: [<same tags as concept>]
source:
  - <same source URL as concept>
generated:
  by: agent:harness
  at: <ISO timestamp>
status: active
---
```

**Reference body structure:**
- `# <Title> — Knowledge Reference`
- `> **Parent concept:** [<concept title>](/concepts/<dom>/<sub>/<slug>.md)` (bidirectional link)
- Full structured knowledge extract organized by the source's natural divisions (parts, chapters, themes)
- For **each concept/principle/technique** mentioned in the source:
  1. **Extract the teaching** — what the source actually says, with examples and key data points
  2. **Expand with grounded sources** — trace each concept to its origin:
     - **Primary sources:** original paper/book author, date, title, journal
     - **Historical context:** who formulated it, when, why
     - **Related work:** key papers that extended or challenged it
  3. **Cross-link** to existing OKF concepts where applicable
- `## Citations` — numbered reference list with full bibliographic entries

**Source expansion methods:**
- `read` Wikipedia articles directly (e.g., `read("https://en.wikipedia.org/wiki/Planning_fallacy")`) — this works even when `web_search` is blocked
- Search existing vault concepts (`okf search "<concept name>"`) for prior knowledge to cross-link
- For well-known concepts (Bayes' theorem, Goodhart's law, etc.), the reference should include: original author, year, paper title, original formulation/quote, key extensions

**When to write a reference file:**
- Video/course/podcast >15 minutes with substantive teachings (not just entertainment)
- Always for educational, technical, or lecture content
- Skip for short clips, entertainment, or news roundup videos

**When NOT to write a reference file:**
- Short videos (<15 min) with a single simple idea — fold into the concept body
- Entertainment content with no extractable knowledge

### Step 6b: Theme Reconciliation
Check whether the newly created concept(s) belong to existing themes and update theme data:

1. Load existing theme structure (do NOT hardcode theme names — they evolve):
   - Read `tools/taxonomy.json` — top-level keys are theme names, values are slug arrays
   - Read `themes/index.md` — maps theme names to concept counts and primary tags
2. Build a tag→theme lookup from the index table's "Primary Tags" column (or from the slugs already in each taxonomy entry)
3. For each new concept (video, tools, creator), check if any of its tags overlap with a theme's characteristic tags:
   - If ≥2 tags match one theme → assign to that theme
   - If exactly 1 tag matches and no other theme fits → assign to that theme
   - If tags match multiple themes → assign to all matching themes
4. For each assignment, append the concept slug to the matching theme's array in `tools/taxonomy.json` — skip if already present, then dedupe+sort the array
5. If 5+ new concepts from one ingest land in the same theme, update the corresponding `themes/<slug>.md` synthesis file (add concepts to Knowledge Synthesis, update concept count) and update `themes/index.md`

### Step 7: Regenerate + Verify
```bash
python3 tools/okf.py index
python3 tools/okf.py lint
python3 tools/okf.py relink --dry-run
python3 tools/okf.py relink  # only if dry-run shows rewrites
```

### Step 8: Log Entry
Append `log.md` entry listing ALL concepts created (video concept + reference file + tools + creator). Note the references/ pattern used.

## Web Article Ingest

### Step 1: Fetch Content
Use `read` tool on the URL to get clean markdown.

### Step 2: Raw Snapshot
Write `raw/web/<slug>.md` with URL, fetch date, full content.

### Step 3: Create Concept
**Apply FBC:** Read the FULL article body to determine what the content is ACTUALLY about. Based on the full body, classify into the correct domain/subdomain/subsubdomain — same FBC process as YouTube ingest. Do NOT rely on keyword matching or tag tables.

If the article links to specific tools worth ingesting separately, extract them as individual concepts (apply FBC for each — same as YouTube tool extraction). If the article has a clear author/creator, create or link to a creator concept.

### Step 3b: Tag Audit
Same as YouTube Step 5b — review tags on the new concept, remove inappropriate ones, add missing meaningful tags from the full body, normalize, ensure ≥1 meaningful tag.

### Step 3c: Backlink + Hub Update
Same as YouTube Steps 5c + 5d — add depth-conditional backlink and update the hub at the correct depth.

### Step 4: Cross-Link + Regenerate + Log
Same as YouTube steps 6b-8.

## Inbox Guardrail
`ingest_inbox.sh` has a **postcondition guardrail**: an inbox item is only moved to `inbox/processed/` if at least one concept was actually created. Failed items (where no concept was created) are moved to `inbox/failed/` with an entry in `inbox/failed/reasons.log` explaining why.

### Purge stale processed files
Every time the inbox is processed, purge any file in `inbox/processed/` that has been sitting there for more than 7 days (mtime-based — `mv` preserves mtime, so it reflects how long the file has been in the folder):

```bash
find "$VAULT/inbox/processed" -maxdepth 1 -type f -mtime +7 -delete
```

Only the `processed` subfolder is purged — never `inbox/failed/`, `raw/`, or `concepts/`.

## Domains / subdomains
See `_config/taxonomy.md` (extensible reference). FBC still required.


## Valid Subsubdomains (reference for FBC — NOT for keyword matching)

| Domain/Subdomain | Subsubdomains |
|-----------------|---------------|
| `learning/dev` | `javascript`, `react`, `css`, `typescript`, `vue`, `svelte`, `html`, `git`, `dotnet`, `api`, `performance`, `ai`, `architecture` |
| `learning/health` | `fitness`, `nutrition` |
| `learning/languages` | `japanese`, `korean`, `chinese`, `german`, `dutch` |
| `learning/skills` | `journaling` |
| `tools/dev` | `general`, `devices` |
| `tools/general` | `devices`, `general`, `orchestration` |

## Visibility Rules
- Default: `shareable` for public content (YouTube, web articles, tools, creators)
- Default: `private` for personal content (journaling, therapy, health, documents)
- `#private` tag → force `visibility: private`
- When unsure → `private`
- `concepts/people/` entries are typically `private` (personal contacts)
- `concepts/creators/` entries are typically `shareable` (public figures)

## Quality Standards
- Read the actual content (transcript, article body) — don't rely solely on descriptions
- Summarize the real thesis, not just the title
- Include concrete examples from the source
- Clean obvious transcription artifacts but note they're verbatim auto-captions
- Every concept must have at least one cross-link to an existing concept when possible
- Tool extraction: only create separate concepts for real tools/products/resources, not social media profiles or generic links
- **Every new concept must be added to its parent hub file** (subdomain hub or domain hub if no subdomain)


## `ingest.py` Caveats

`tools/ingest.py` has known limitations requiring a **mandatory post-ingest cleanup pass**:

1. **Slug-from-path bug**: When `parse_source()` fails to extract a title, `slugify()` falls back to `source_ref` (file path), producing garbage slugs (`users-ima-okf-inbox-...`). Always verify and rename slugs post-ingest.
2. **No subdomain support**: `ingest.py` only accepts `--domain`. Subdomain routing must be done post-ingest via file moves.
3. **Title extraction may fail**: Obsidian clipping frontmatter produces mangled titles. Inspect all frontmatter after ingest.
4. **Post-ingest cleanup is mandatory**: Rename garbage slugs → move to correct subdomain/subsubdomain paths → fix frontmatter → delete true duplicates (check existing file quality for merge).
5. **Title extraction from headings**: Skip structural headings (`Transcript`, `Features`, `Installation`, `Readme`, `Overview`, `Introduction`) — not suitable concept titles.
