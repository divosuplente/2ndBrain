---
type: skill
name: okf-batch-ingest
description: "Ingest a folder of markdown files (and attachments) into the OKF brain."
---

# OKF Folder Ingest — Batch Markdown + Attachments

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


## Trigger Patterns
- User drops a folder path or says "ingest this folder"
- User has markdown files with attachments they want processed
- User says "process these files" or "add these to the brain"
- User references a folder with `.md` files and/or images/PDFs

## Execution Pattern
Use the eval kernel for batch processing — subagents on smaller models fail at complex OKF extraction/writing tasks. Process in batches of 10. **Split by job: routing/classification decisions go to the `judge` decision model (`judge_batch`, Step 2); content writing (body, title, description, tags) goes to `completion()`.**

```
# Correct pattern
results = parallel([lambda f=f: process_one(f) for f in batch])

# Wrong pattern — do NOT use task subagents for extraction or concept writing
```

## Workflow

### Step 1: Scan the Folder
```
Read the directory tree
- Identify all .md files (primary content)
- Identify all attachments (images, PDFs, other docs)
- Read each .md file to understand its content type
- Skip duplicate files: numbered suffixes (" 1.md", " 2.md"), content duplicates
- Skip hub files (domain/subdomain hubs are structural, not source content)
```

### Step 2: Classify Each File with the judge model (Full-Body Classification)

Routing decisions — `type`, `domain`, `subdomain`, `subsubdomain`, `visibility`, and `action` (create/update/skip) — are typed judgments and MUST be made by the eval kernel's `judge_batch` (TypeSafe decision model) over each file's **full body**. The judge returns only closed-set choices — it cannot emit a free-form path like `tools/agents/mcp` — so every option set is a closed label list: the 10 domains, the unified type vocabulary, and the known leaves from `_config/taxonomy.md` (`@tax`), plus an explicit `new-leaf` escape label that routes to the taxonomy extension protocol when nothing fits durably. Never route on filename, title keywords, or keyword tables: the judge sees the body, and the rubric below is encoded as its criteria.

**One `judge_batch` per run** — every file is one state, all states answer the same questions:

- **State is contentful**: the file's full body including frontmatter (long files: head (first ~40 lines) + all H1/H2 headings). Never feed only title/filename — that bakes in the filename-derived classification failure this step exists to kill.
- Append to each state: `CANDIDATES: <top okf search hits for the title, path + one-line description each>` so `action` can be judged from one call.

```python
questions = {
  "type": {"type": "choice",
           "instructions": "Which unified type best fits this concept? Judge from the full body, not the filename.",
           "criteria": {"tool": "software/product/CLI/app/device being described",
                        "skill": "agent/harness skill or reusable procedure",
                        "spec": "standard/protocol/interface specification",
                        "person": "an individual",
                        "organization": "a company or group",
                        "document": "identity/legal/personal record",
                        "topic": "general topic, no concrete artifact",
                        "learning": "course, study plan, learning resource",
                        "reference": "lookup material: glossary, table, list",
                        "playbook": "step-by-step runbook/playbook",
                        "project": "ongoing project/effort",
                        "goal": "set target or outcome",
                        "habit": "routine or repeated practice",
                        "key-element": "recurring element/attribute in a system",
                        "source": "a source itself (video, article, recording)",
                        "note": "fallback when nothing else fits"}},
  "domain": {"type": "choice",
             "instructions": "Which ONE domain does this source describe? Judge from the full body, not the filename.",
             "criteria": {  # rubric = the domain table below
                 "life": "ONLY personal: neurodivergent, therapy, journaling, personal goals, travel, mindfulness",
                 "learning": "non-personal content: recipes, health, exercise, dev articles, languages, music, keto, etc.",
                 "tools": "GitHub repos, apps, devices, software tools",
                 "skills": "AI-related skill/agent-skill content",
                 "specs": "AI-related specifications",
                 "people": "private contacts", "orgs": "organizations",
                 "documents": "identity/records", "work": "work logs, enterprise notes",
                 "creators": "public content creators"}},
  "subdomain": {"type": "choice",
                "instructions": "Which subdomain under the chosen domain? Labels are the domain's known leaves from `_config/taxonomy.md`. Choose 'flat' if the concept belongs directly in the domain; 'new-leaf' only if no existing leaf fits durably.",
                "criteria": {"flat": None,
                # runtime: read the "Known subdomains" row for this domain from `_config/taxonomy.md` →
                # one label per leaf; +"new-leaf": "no existing leaf fits — extend the taxonomy (sanity gate below)"
                }},
  "subsubdomain": {"type": "choice",
                   "instructions": "Which subsubdomain under (domain, subdomain)? Same rules: known leaves from `_config/taxonomy.md` only, 'flat' when none apply, 'new-leaf' when nothing fits durably.",
                   "criteria": {"flat": None,
                   # runtime: read the "Known subsubdomains" row for this path from `_config/taxonomy.md` →
                   # one label per leaf; +"new-leaf": "no existing leaf fits — extend the taxonomy (sanity gate below)"
                   }},
  "visibility": {"type": "choice",
                 "instructions": "private for personal/sensitive content, shareable otherwise; when unsure, private.",
                 "criteria": {"private": "personal/sensitive — never exported",
                              "shareable": "eligible for shareable export"}},
  "action": {"type": "choice",
             "instructions": "Decide using the CANDIDATES list. 'skip-duplicate' ONLY when a candidate is the same source already ingested.",
             "criteria": {"create": "no existing concept covers this",
                          "update-existing": "same project/tool/person as a candidate — extend it, append source",
                          "skip-duplicate": "a prior ingest of this exact source already created the concept"}},
}
b = judge_batch(states, questions, intent="okf-batch-classify")
# pull across cells: await b.drain(timeout) until b.status()["running"] == 0; inspect b.failed()
```

**Sanity gate before writing (judge output is a recommendation, not law):**
- Labels must come from the closed sets (unified type vocabulary + `_config/taxonomy.md` leaves). Unknown label → re-judge that one state with the valid labels enumerated.
- `new-leaf` (subdomain or subsubdomain) → run the taxonomy extension protocol in `_config/taxonomy.md` BEFORE writing: kebab-case leaf name, create hub file, update `@tax`, `log.md` entry when durable. Never force-fit a wrong leaf.
- `action: skip-duplicate` needs the matched candidate in the state's CANDIDATES; low confidence → treat as `create` and let Step 6 canonicalize resolve the collision.
- Every state must answer; a failed state → classify it manually before writing, never leave a file unrouted.

For each file, after the batch resolves:
1. **Check existing frontmatter** — parse it if present (feeds the state and the title/slug derivation)
2. **Derive slug** from the frontmatter `title` or a real title in the body (never from a mangled clipping filename): format `{owner}-{name}` lowercase with hyphens
3. **Check for existing concept** with that slug → the judge's `action` already covered candidates; a slug hit still means merge, never overwrite

**Rubric for the criteria (encode in the question text verbatim):**

| Domain | Content scope | Visibility default |
|--------|--------------|-------------------|
| `life` | **ONLY** personal: neurodivergent, therapy, journaling, personal goals, travel, mindfulness | private |
| `learning` | Everything else: recipes, cooking, health, exercise, dev articles, languages, music, aromatherapy, keto | shareable |
| `tools` | GitHub repos, apps, devices, software | shareable |
| `skills` | AI-related content | shareable |
| `specs` | AI-related specifications | shareable |
| `people` | Private contacts | private |
| `orgs` | Organizations | private |
| `documents` | Identity/records | private |
| `work` | Work logs, enterprise notes | private |
| `creators` | Public content creators | shareable |

**Personal domains** (`life`, `people`, `orgs`, `documents`, `work`) default to `visibility: private`. All other domains default to `shareable`. When unsure, treat as `private`.

**Subdomain routing labels** are the closed sets from `_config/taxonomy.md` (`@tax`) — the current map, loaded fresh each run; do not re-paste or hardcode tables here (the file is extensible and authoritative). Never route on keyword tables.

**Critical domain routing changes** (vs. legacy) — apply to the rubric text and to any manual override:
- `cooking`, `health`, `keto`, `aromatherapy` → `learning/`, NOT `life/`
- `life/` is **ONLY** neurodivergent, personal, travel, mindfulness
- `tools/health` → distributed: devices stay in `tools/dev/devices/`, apps/content go to `learning/health/`
- `tools/dev/coding-agents` → `tools/agents/coding-agents`
- `tools/dev/orchestration` → `tools/agents/orchestration`

### Step 2b: Tag Audit (During Ingest)

After reading the full body for classification, **review tags for each file**:

- Remove `youtube`, `clippings`, and domain-redundant tags (e.g., a `learning` tag on a concept already in `learning/`)
- Normalize all tags to **lowercase + hyphen** format
- **Add missing meaningful tags** derived from the full body content
- Normalize near-duplicates per tag conventions (singular preferred, consistent hyphenation)
- Ensure **at least one meaningful tag** per concept
- Remove garbage/hex/numeric-only tags
- Keep the `dev` tag (add specific sub-topic tags like `javascript`, `typescript`, `react` alongside when applicable)
- Avoid singular/plural duplicates — pick one form (singular preferred)

### Step 3: Pre-flight Ledger
Before writing any concept files, build and reconcile the full plan:
1. For every file, record: filename → slug → domain → action (CREATE/SKIP)
2. Check every intended slug against existing concepts (glob + `okf search`)
3. Check for same-prefix collisions (`owner-repo` vs `owner-repo-the-full-name`)
4. Reconcile totals — every file accounted for as CREATE or SKIP with reason
5. Only then start writing

### Step 4: Process Attachments
- Images referenced in markdown → note their paths
- If images need to be in the repo, copy to `raw/attachments/<slug>/` and rewrite paths
- PDFs → note as references in the concept body
- If the folder contains attachments not referenced by any .md, create a `note` concept listing them

### Step 5: Batch Create
For each file, run the standard OKF ingest pipeline via `completion()` + `parallel()`:
1. Create or update canonical concept at `concepts/<domain>/<subdomain>/<slug>.md` (or `concepts/<domain>/<subdomain>/<subsub>/<slug>.md` for subsubdomains, or `concepts/<domain>/<slug>.md` if no subdomain)
2. Write frontmatter: `type`, `visibility`, `title`, `description`, `domain`, `tags`
3. Write body preserving the original content structure
4. If external source, snapshot to `raw/`

### Step 6: Canonicalize (serial — the barrier)

**This is the critical step.** After ALL concepts are created, resolve conflicts before proceeding. Do NOT skip — this prevents duplicate pages with divergent content.

1. **Slug collision detection** — run `okf index` + `okf lint` and check for `duplicate` and `slug-collision` warnings. Also glob `concepts/<domain>/` and `concepts/<domain>/<subdomain>/` for same-prefix slugs.
2. **Resolve collisions** — for each pair:
   - Short slug is authoritative (correct frontmatter)
   - Old long-slug versions have outdated metadata (`type: note`, `visibility: private`, no tags)
   - Compare old vs new content: if old has unique substance not in new, merge it in first
   - Delete the old long-slug file, keep the short slug
3. **Tag normalization** — for all newly created concepts:
   - Tags are lowercase, hyphenated, singular (e.g., `self-hosting` not `Self-Hosting`)
   - Normalize plurals: agents→agent, skills→skill, tools→tool, orgs→org, hormones→hormone, languages→language, loops→loop, standards→standard, supplements→supplement, containers→container, coding-agents→coding-agent, agent-skills→agent-skill, surfaces→surface
   - **Reorg merge rules** (established during vault reorganization):
     - `ai-skills` → `ai`
     - `agenticcoding` → `agentic-engineering`
     - `blood-sugar` → `glucose`
     - `health-metric` → `health`
     - `api-keys` → `api`
     - `blood` → `blood-pressure`
     - `liver` → `liver-health`
     - `webassembly` → `wasm`
     - `cardio` → `cardiovascular`
     - `supplement` → `supplements`
     - `hormone-disruptor` → `endocrine-disruptor`
     - `ldl-cholesterol` → `ldl`
     - `hdl-cholesterol` → `hdl`
     - `insulin-resistance` → `metabolic-health`
     - `heart` → `cardiovascular`
     - `cholesterol` → `lipids`
     - `blood-pressure-medication` → `antihypertensive`
     - `testosterone-supplement` → `testosterone`
     - `omega3` → `omega-3`
     - `vitamin-d3` → `vitamin-d`
   - Remove domain-redundant tags (a `domain: tools` concept doesn't need a `tools` tag)
   - Remove the `clippings` tag (Obsidian clipping artifact, no meaning)
   - Remove the `youtube` tag (no meaningful information)
   - Remove domain-name tags (e.g., a concept in `learning/` doesn't need a `learning` tag)
   - Ensure at least one tag per concept
4. **Title/description check** — verify each new concept has a real title (not just the filename) and a one-line description
5. **Thin page detection** — flag concept pages with body < 100 chars. Re-generate thin pages with a tighter prompt, or merge into a parent concept if too insubstantial.

### Step 7: Normalize (content-specific)

Apply only where provenance indicates the source type:

- **YouTube/video transcripts** — ASR normalization: fix known auto-caption garbles (e.g., "Enthropic"→"Anthropic", "pantic AI"→"Pydantic AI", "Lang graph"→"LangGraph"). Fix ONLY proper nouns and technical terms. Preserve all other wording exactly.
- **GitHub READMEs** — normalize heading depth (ensure `##` as top body level, not `#`), strip raw HTML if markdown equivalent exists.
- **Article clippings** — remove boilerplate (cookie notices, signup CTAs, nav text) that may have been captured.

Do NOT apply transcript-specific ASR fixes to non-transcript content.

### Step 8: Cross-Link + Hub Update
- After all concepts are created and canonicalized, search for cross-links between them
- Use `okf search` to find related existing concepts
- Add bidirectional links where relevant — use bundle-relative paths:
  - `/concepts/<domain>/<slug>.md` for flat domain concepts
  - `/concepts/<domain>/<sub>/<slug>.md` for subdomain concepts
  - `/concepts/<domain>/<sub>/<subsub>/<slug>.md` for subsubdomain concepts
- Ensure `## Related` sections use typed subheadings with one-line descriptions rather than bare link lists
- **Depth-conditional backlinks** — every new concept MUST link back to its parent hub:
  - Subsubdomain concept → backlink to `concepts/<domain>/<sub>/<subsub>.md` (the subsubdomain hub)
  - Flat subdomain concept (no subsub) → backlink to `concepts/<domain>/<sub>.md` (the subdomain hub)
  - Flat domain concept (no subdomain) → backlink to `concepts/<domain>/<domain>.md` (the domain hub)
- **Update hub files** — for each new concept, add a link entry to the appropriate hub per the depth rules above

### Step 8b: Theme Reconciliation
After cross-linking, check whether newly created concepts belong to existing themes and update theme data:

1. Load existing theme structure (do NOT hardcode theme names — they evolve):
   - Read `tools/taxonomy.json` — top-level keys are theme names, values are slug arrays
   - Read `themes/index.md` — maps theme names to concept counts, domains spanned, and primary tags
2. Build a tag→theme lookup from the index table's "Primary Tags" column (or from the slugs already in each taxonomy entry: scan existing slugs, read their frontmatter tags, and group)
3. For each newly created concept, check if any of its tags overlap with a theme's characteristic tags:
   - If ≥2 tags match one theme → assign to that theme
   - If exactly 1 tag matches and no other theme fits → assign to that theme
   - If tags match multiple themes → assign to all matching themes
4. For each assignment, append the concept slug to the matching theme's array in `tools/taxonomy.json` — skip if already present, then dedupe+sort the array
5. If a cluster of 10+ new concepts shares tags that don't fit any existing theme, create a new theme key in `tools/taxonomy.json`
6. If 5+ new concepts land in one theme, update the corresponding `themes/<slug>.md` synthesis file:
   - Add new concepts to the relevant Knowledge Synthesis sub-section
   - Update the concept count in the Overview
   - Update `themes/index.md` concept count for that theme's row

### Step 9: Regenerate + Verify
```bash
python3 tools/okf.py index
python3 tools/okf.py lint
python3 tools/okf.py relink --dry-run
python3 tools/okf.py relink  # only if dry-run shows rewrites
```
Fix any new lint regressions introduced by this batch (broken links to newly created concepts, missing required frontmatter on new files, tag violations). Pre-existing warnings are non-blocking — `lint` is report-only and broken links are tolerated per project contract. Re-run `okf index` + `okf lint` after fixes.

### Step 10: Update Catalog
- Update `index.md` concept counts and domain breakdown — derive counts from `okf.py index` output (canonical `tools/index.json`), not from ad-hoc file counting
- Append a single `log.md` entry for the batch:
```
## [YYYY-MM-DD] batch-ingest | <folder description>
- <N> concepts created from <folder path>
- <list of concept IDs created>
- <slug collisions resolved>
- <cross-links added>
- <attachment handling notes>
- <verification results>
- Corpus: <old_count> → <new_count> concepts (+<net> net).
```

### Step 11: Cleanup
- **Only move inbox files to `inbox/processed/` if ≥1 concept was actually created** — this is the postcondition guardrail
- **Failed items** → move to `inbox/failed/` with a `reasons.log` explaining why each item failed (no concept created, parse failure, etc.)
- Delete skipped duplicate files from the folder
- **Purge stale processed files** — delete any file in `inbox/processed/` that has been sitting there for more than 7 days (mtime-based; `mv` preserves mtime, so it reflects time in the folder). This is a maintenance step to keep the archive lean and runs every batch:
  ```bash
  PURGED=$(find "$VAULT/inbox/processed" -maxdepth 1 -type f -mtime +7 -delete -print | wc -l | tr -d ' ')
  echo "Purged $PURGED processed file(s) older than 7 days."
  ```
  Never purge `inbox/failed/`, `raw/`, or `concepts/` — only the `processed` subfolder.
- Re-run `okf index` + `okf lint` one final time to confirm no new regressions from this batch
- **Auto cross-link** — run `python3 tools/okf.py link --auto --max 50 --min-score 50 --quiet` after the final index to connect new concepts to existing vault knowledge. Skips already-linked pairs and noisy hubs.

## Tag Handling
- If user provides `#hashtags` with the folder request, apply them to ALL concepts in the batch
- If individual files have their own tags in frontmatter, merge with batch tags
- `#private` on the batch → all concepts `visibility: private`

## Duplicate Detection
- Before creating each concept, run `okf search "<title>"` to check if it exists
- If a concept with the same slug exists → update it (merge content, append new source)
- Never overwrite curated content — append only
- Numbered-suffix files (` 1.md`, ` 2.md`) are always duplicates of the unnumbered version — skip them
- Content duplicates (same tool/project described differently) — keep the better slug/metadata version

## Attachment Types
- `.md` → primary content
- `.png`, `.jpg`, `.jpeg`, `.gif`, `.svg` → attachments, copy to `raw/attachments/`
- `.pdf` → reference, note path in concept body
- `.csv`, `.json` → data files, note path in concept body
- Other → note as reference, skip binary processing

## Comparison with okf-ingest-channel

| Aspect | okf-batch-ingest | okf-ingest-channel |
|--------|------------------|-------------------|
| Scope | Folders of mixed markdown | Entire YouTube channel |
| Source types | GitHub READMEs, article clippings, notes, videos | YouTube transcripts only |
| Classification | `judge` decision model (`judge_batch`) routing per rubric; full-body read, not keywords | Tag-based transcript routing |
| Canonicalization | Slug collision + tag/title normalization with reorg merge rules | Cross-video concept merging + taxonomy |
| ASR normalization | Only for video-provenance items | Always (all sources are transcripts) |
| Best for | Inbox processing, clippings, mixed folders | Building a knowledge domain from a creator's catalog |


## `ingest.py` Caveats

`tools/ingest.py` has known limitations requiring a **mandatory post-ingest cleanup pass**:

1. **Slug-from-path bug**: When `parse_source()` fails to extract a title, `slugify()` falls back to `source_ref` (file path), producing garbage slugs (`users-ima-okf-inbox-...`). Always verify and rename slugs post-ingest.
2. **No subdomain support**: `ingest.py` only accepts `--domain`. Subdomain routing must be done post-ingest via file moves.
3. **Title extraction may fail**: Obsidian clipping frontmatter produces mangled titles. Inspect all frontmatter after ingest.
4. **Post-ingest cleanup is mandatory**: Rename garbage slugs → move to correct subdomain/subsubdomain paths → fix frontmatter → delete true duplicates (check existing file quality for merge).
5. **Title extraction from headings**: Skip structural headings (`Transcript`, `Features`, `Installation`, `Readme`, `Overview`, `Introduction`) — not suitable concept titles.
