---
type: skill
name: okf-query
description: "Query and search the OKF brain vault. Triggered on 'what do I know about X', 'search for', 'find concepts'."
---

# OKF Query — Search & Recall from the Brain

## Trigger Patterns
- User asks "what do I know about X"
- User says "search for", "find", "look up"
- User asks "do I have anything on X"
- User says "what's in the brain about X"
- User wants to recall or review prior knowledge

## Workflow

### Step 1: Search
```bash
python3 tools/okf.py search "<user's query>"
```
Review results ranked by relevance.

### Step 2: Read Concepts
Read the top-ranked concept files to gather the full picture:
- Start with the highest-scoring result
- Follow cross-links to related concepts
- Optionally read `index.md` for domain overview

### Step 3: Answer
- Answer the user's question using the concept content
- **Cite exact concept files** used (e.g., "According to `concepts/learning/create-your-japanese-brain.md`...")
- If nothing supports the answer: say so plainly. Do not fabricate.
- Distinguish between `private` and `shareable` — never expose private content in a shareable context

### Step 4: File Back (Optional)
If the answer synthesizes multiple concepts into a new durable insight:
1. Create a new concept (`concepts/<domain>/<slug>.md`)
2. Set `visibility` appropriately
3. Cross-link to the source concepts
4. Run the standard pipeline (index, lint, relink, log)
5. Tell the user: "Filed this as a new concept: `concepts/learning/<slug>.md`"

## Search Tips
- `okf search "japanese brain"` — full-text BM25 search
- `okf search "agents" --domain tools` — filter by domain
- `okf search "private" --visibility shareable` — filter by visibility
- `okf search "tool" --type tool` — filter by type
- Multiple results → read top 3-5 for comprehensive answer
