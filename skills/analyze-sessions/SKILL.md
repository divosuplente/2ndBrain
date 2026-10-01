---
type: skill
name: analyze-sessions
description: "Analyze past OMP agent sessions: search transcripts, render sessions as markdown, mine prompting patterns, and roll up costs."
---

# Analyze Sessions

Tools for querying past OMP sessions. All scripts are stdlib Python 3, no dependencies. Sessions live at `~/.omp/agent/sessions/<project>/`.

## Data shape

Each session is a JSONL file. Records are:
- `session` — header with `cwd`, `id`, `timestamp`, `title`
- `message` — user/assistant text; assistant messages include `usage` with `cost.total` and `totalTokens`


All scripts in `skills/analyze-sessions/scripts/`. Run from anywhere:

```bash
python3 ~/.omp/agent/skills/analyze-sessions/scripts/<script>.py [args]
```

### `cost.py` — cost rollups

```bash
# Last 7 days, by day (default)
python3 cost.py --since 7d

# Last 30 days, top 10 projects
python3 cost.py --since 30d --by project --limit 10

# 10 most expensive sessions
python3 cost.py --since 30d --by session --limit 10

# Grand total
python3 cost.py --since 30d --by total
```

Groupings: `total`, `day`, `project`, `session`.

### `search.py` — search across transcripts

Substring or regex search across user and assistant message text.

```bash
python3 search.py "session kickoff"
python3 search.py "how to" --in user --since 30d
python3 search.py --regex "TODO\(.+\)"
python3 search.py "auth error" --context 2
python3 search.py "deploy" --cwd /Users/ima/okf
```

### `show_session.py` — render one session as markdown

```bash
python3 show_session.py --latest
python3 show_session.py --session 01a0378d
python3 show_session.py --latest --cwd /Users/ima/okf
python3 show_session.py --session 01a0378d --include-subagents
```

### `prompts.py` — dump user prompts for pattern mining

```bash
python3 prompts.py --since 30d
python3 prompts.py --since 7d --max-chars 1500 --format jsonl
python3 prompts.py --cwd /Users/ima/okf --since 30d
python3 prompts.py --grep "rate limit" --since 60d
```

## Shared filters

Available on **all scripts**:

| Flag | Meaning |
|---|---|
| `--since WHEN` / `--until WHEN` | `YYYY-MM-DD`, or relative: `7d`, `2w`, `3h` |
| `--cwd SUBSTR` | Filter by session cwd |
| `--session ID` | Session id prefix (8 chars) |
| `--limit N` | Cap results |
| `--in {user,assistant,all}` | Role filter (search.py only) |

## Common queries

| Question | Command |
|---|---|
| Find old session about X | `python3 search.py "X"` |
| What did I do yesterday | `python3 show_session.py --latest --since 1d` |
| Patterns in my prompting | `python3 prompts.py --since 30d --max-chars 1500` |
| Daily cost trend | `python3 cost.py --since 30d --by day` |
| Most expensive project | `python3 cost.py --since 30d --by project --limit 5` |
