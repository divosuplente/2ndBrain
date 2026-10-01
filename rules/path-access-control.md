---
type: infrastructure
---

# Path Access Control Rule

**Purpose**
Define which paths agents may access by default, and which require explicit user permission.

**Allowed Paths (read/write)**

- `skills/` – usable agent skills (includes stage contracts under `skills/*/stages/`)
- `concepts/` – OKF concept files (the vault's core content)
- `inbox/` – temporary ingest staging
- `tools/` – CLI tooling (`okf.py`, `ingest.py`, migration scripts)
- `provenance/` – provenance maps and generated artifacts
- `_config/` – ICM short reference slices (conventions, glossary)
- `themes/` – cross-domain synthesis overlays
- `specs/` – feature specs
- `books/` – per-chapter book slices + formula snapshots (committed reference material; originals stay outside the repo)
- `teaching/` – OKF teach workspaces (`_MISSION.md`, `_LEARNING-PATH.md`, `lessons/`, `assets/`)
- `.omp/` – project-local OMP extensions
- `~/.omp/` – global OMP configuration and extensions
- `~/.agents/` – global agent harness skills and rules
- `rules/` – agent path/policy rules (this file)
- `./` – vault root (repo-root scripts and files: `teach`, `README.md`, `ADOPTIONS.md`, etc.)
- `.github/`
- `IDENTITY.md`, `CONTEXT.md`, `AGENTS.md`, `decisions.md` – orientation + contract + decisions
- `log.md` – vault change log
- `index.md` – vault catalog (prefer `okf.py index` to regenerate; hand-edits will be overwritten)
- `/Users/ima/2ndbrain/` – sister vault (template sync target)
- `xd://` – all internal OMP tool device paths (propose, report_issue, security_scan, ast_grep, ast_edit, debug, github, lsp, inspect_image, browser, checkpoint, rewind, memory_edit, retain, reflect, recall, unfold, generate_image, tts, mcp__*)

**Read-Only Paths**

- `raw/` – verbatim source snapshots (archive; never edit)

**Blocked Paths**

- Any path that resolves outside the repo root requires explicit user permission before access
- Any path not listed above requires explicit user permission before access.

**Enforcement**

- Agents **MUST** check this rule before accessing paths not listed under **Allowed** or **Read-Only**.
- If a path is blocked, the agent must either abort the operation or request explicit user permission.
- User-granted permission for a specific task carries across the session — no need to re-confirm for each file within the approved scope.
