---
type: skill
name: wikiskill
description: "Evolve agent skills using the WikiSkill loop: co-evolve skills with a persistent knowledge base (wiki) that compounds across iterations."
---

# WikiSkill Skill Evolution Loop

Implements the WikiSkill framework (Tang et al., 2026): co-evolve agent skills with a persistent knowledge base. The wiki compounds across iterations; skills are gated by validation and rolled back on failure.

## When to Use

- User wants to evolve/improve a skill from execution traces or benchmarks
- "Optimize this skill using WikiSkill", "evolve skills from experience", "iterative skill improvement"
- You have a target skill, a training set of tasks, and a validation metric

## Workspace Layout

For each evolution run, create a working directory (e.g., `.wikiskill/<target-skill>/`):

```
raw/                    # Immutable execution traces, appended each iteration
wiki/
  patterns/             # Failure modes, strategies, actionable workarounds
  logs.md               # Evolution history (appended each iteration)
  skill-impact.md       # Proposal diffs + validation outcomes + acceptance
  index.md              # Pattern catalog
skills/
  <target>/SKILL.md     # Active skill being evolved
  <target>/PURPOSE.md   # Motivating wiki patterns
```

## Procedure

### 0. Preflight

1. **Identify inputs:**
   - Target skill path (existing or new)
   - Training tasks (dataset, bench, or user-provided scenarios)
   - Validation tasks (separate from training)
   - Scoring function / metric
   - Max iterations `K` (default 8)

2. **Initialize workspace:**
   - Create directory structure above
   - Seed `wiki/logs.md`, `wiki/skill-impact.md`, `wiki/index.md` as empty
   - Copy target skill to `skills/<target>/SKILL.md` (or create empty)
   - Create `skills/<target>/PURPOSE.md` with initial motivation

3. **Baseline validation:**
   - Run inference agent on validation tasks with current skill (or empty)
   - Record `R_best` = baseline validation score

### 1. Iteration Loop (k = 1...K)

For each iteration, execute four phases sequentially:

#### Phase 1: Inference Rollout

Run the inference agent on training tasks using current skills. The agent has access to active skills but **not** the wiki layer.

- Spawn subagent(s) via `task` to execute training tasks
- Capture full traces: reasoning, tool calls, outputs, final answers
- Write traces to `raw/iteration-{k}/` as JSON or markdown
- Record pass/fail per task against ground-truth or scoring function

#### Phase 2: Wiki Maintenance

Analyze sampled traces + existing wiki to update persistent knowledge.

- **Sample traces:** Stratified selection — up to 5 failing traces + up to 3 passing traces from current iteration
- **Spawn Wiki Maintainer subagent** via `task` with:
  - Full `wiki/` contents (patterns, logs, index)
  - Sampled traces (cap each at ~15K chars)
  - Instructions: root-cause failures, extract successful strategies, create/update pattern files, update `index.md`, append to `logs.md`
- Wiki Maintainer outputs updated wiki files (never deletes; only creates/appends/updates)

#### Phase 3: Skill Proposal

Generate an atomic skill proposal informed by the wiki.

- **Spawn Skill Proposer subagent** via `task` with:
  - `wiki/index.md` (pattern catalog)
  - `wiki/skill-impact.md` (full history of proposals, diffs, scores, outcomes)
  - Training outcome summary (pass/fail per task, predictions vs. ground-truth)
  - Current skill content at `skills/<target>/SKILL.md`
  - Access to `wiki/patterns/` and `raw/` via read tools (ReAct: reads specific patterns/traces on demand)
- Skill Proposer produces **one atomic proposal** targeting a single skill:
  - Create new skill, OR
  - Patch-based edit to existing skill
- Proposal includes: target file, diff/content, brief rationale referencing wiki patterns

#### Phase 4: Gating & Rollback

Validate and gate the proposal.

1. Apply proposal → candidate skill set
2. Run inference agent on validation tasks with candidate skills
3. Compute validation score `R(T_val,k)`
4. **Decision:**
   - If `R(T_val,k) > R_best`:
     - Accept: update `skills/<target>/SKILL.md`
     - Update `R_best = R(T_val,k)`
     - Record outcome: `Accepted`
   - Else:
     - Reject: keep `skills/<target>/SKILL.md` unchanged (rollback)
     - Record outcome: `Rejected`
5. **Always** append to `wiki/skill-impact.md`: iteration, proposal diff, validation score, acceptance outcome
6. **Wiki is never rolled back** — accumulated knowledge persists regardless

#### Early Termination

If `R_best = 1.0` (perfect validation), break the loop and return final results.

### 2. Final Output

After K iterations or early termination:

1. **Deliverables:**
   - Final evolved skill(s) at `skills/<target>/`
   - Evolution log: `wiki/logs.md`
   - Pattern catalog: `wiki/patterns/` + `wiki/index.md`
   - Audit trail: `wiki/skill-impact.md`
   - Score trajectory: `R_best` progression per iteration

2. **Summary report:**
   - Initial vs. final validation score
   - Number of accepted/rejected proposals
   - Key patterns discovered
   - Skill diff (initial → final)

## Subagent Design

### Wiki Maintainer

```
Task: Analyze execution traces against the existing wiki.
Input: wiki/*, sampled traces (≤8, stratified)
Output: Updated wiki/patterns/*.md, wiki/index.md, wiki/logs.md
Rules:
- Create new pattern files for novel failure modes or strategies
- Update existing patterns with new evidence
- Never delete patterns; mark outdated ones as superseded
- Append to logs.md with iteration summary
- Keep patterns concise: diagnosis, evidence, actionable workaround
```

### Skill Proposer

```
Task: Propose one atomic skill edit informed by wiki and traces.
Input: wiki/index.md, wiki/skill-impact.md, training outcomes, current skill
Tools: read specific wiki/patterns/*.md and raw/ traces on demand
Output: Atomic proposal (target file, content/diff, rationale)
Rules:
- Target exactly one skill per iteration
- Reference specific wiki patterns in rationale
- Consult skill-impact.md to avoid repeating rejected proposals
- Proposals should be surgical: minimal edit that addresses identified root cause
- If no improvement opportunity exists, propose no-op
```

## Configuration

| Parameter | Default | Notes |
|-----------|---------|-------|
| `K` (iterations) | 8 | Early termination on perfect validation |
| Failing trace sample | 5 | Root-cause analysis |
| Passing trace sample | 3 | Strategy extraction / regression prevention |
| Trace char cap | 15,000 | Prevents context overflow |
| Gating criterion | Strict improvement | `R > R_best`; neutral proposals rejected |
| Wiki pruning | None | Acknowledged limitation; manual cleanup if wiki grows large |

## Notes

- The wiki is the knowledge compounder. Even rejected proposals feed its audit trail, preventing the proposer from repeating failed approaches.
- The inference agent does **not** see the wiki during rollouts. This forces skill quality gains rather than wiki-dependent crutches.
- Each iteration's proposal targets a single skill. Multi-skill changes require multiple iterations.
- Adapt trace format to your execution environment (JSON logs, session transcripts, or benchmark outputs).

## Reference

Tang et al. (2026). WikiSkill: Compiling Agent Experience into Persistent Knowledge for Skill Evolution. arXiv:2608.27454. See [/concepts/tools/agents/wikiskill.md](/concepts/tools/agents/wikiskill.md).
