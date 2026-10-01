---
type: skill
name: okf-problem-journal
description: "Create, review, and query worked problem entries in the OKF vault. Problems are first-class concepts under concepts/learning/<domain>/problems/."
---

# OKF Problem Journal Skill

## Trigger Patterns
- "record problem"
- "log problem"
- "problem journal"
- "review my mistakes"
- "problem stats"
- "I solved a problem"
- "/problem"

## Procedures

### CREATE — Record a new problem

1. Determine domain and sub-topic from the problem content
2. Create concept at `concepts/learning/<domain>/problems/<slug>.md`
3. Use template from `concepts/learning/_templates/problem.md`
4. Frontmatter:
   - `type: playbook`
   - `visibility: private`
   - `tags: [<domain>, <sub-topic>, problem]`
   - `difficulty: 1-5` (self-rated; 1 = trivial, 5 = needed significant help)
   - `solved: false` initially; set `true` once complete
   - `attempts: 0` initially; increment on each attempt
5. Body follows Pólya structure: Statement → Approach → Solution → Key Insight → Mistakes → Related
6. Cross-link: add link from the parent topic concept to this problem
7. Run `python3 tools/okf.py index`

### REVIEW — Review past mistakes before a session

1. `python3 tools/okf.py search "problem" --domain learning` or grep for `type: playbook` under `concepts/learning/*/problems/`
2. Filter to problems where `solved: false` OR `difficulty >= 3` OR `attempts > 1`
3. Present the "Mistakes" sections as a pre-session checklist
4. This is deliberate practice: review where you failed before starting new problems

### STATS — Problem statistics

1. Count total problems by domain
2. Count by difficulty bucket (1-2 easy, 3 medium, 4-5 hard)
3. Count unsolved vs solved
4. Count multi-attempt problems (deliberate practice targets)
5. Present as a table

### CLOSE — Mark a problem solved

1. Find the problem concept
2. Set `solved: true` in frontmatter
3. Ensure "Solution" and "Key Insight" sections are filled
4. Update `timestamp`

## Conventions

- Problem slugs: `<topic>-<brief-description>.md` (e.g., `lagrangian-double-pendulum.md`)
- The "Mistakes" section is the most valuable part — it prevents repeating the same error
- Always link problems back to their parent topic concept
