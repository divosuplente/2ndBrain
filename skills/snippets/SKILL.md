---
type: skill
name: snippets
description: "Quick behavioral snippets activated via /snippet commands. Injects into system prompt every turn until cleared."
---

# Snippets

Quick behavioral contracts injected into the system prompt via the `before_agent_start` extension hook. Data lives in `~/.omp/agent/snippets.json`.

## Commands

| Command | Effect |
|---|---|
| `/snippet` or `/snippet list` | List all available snippets with descriptions |
| `/snippet help` | Show usage instructions |
| `/snippet <name>` | Activate snippet (injects into system prompt every turn) |
| `/snippet <name1> <name2>` | Activate multiple snippets |
| `/snippet add <name> "<desc>" "<body>"` | Create a new snippet |
| `/snippet remove <name>` | Remove a snippet |
| `/snippet off` | Clear active snippets |

## Default Snippets

| Name | Description |
|---|---|
| `verify` | Verify, don't assume |
| `diagnose` | Diagnose, don't fix |
| `delegate` | Delegate exploration |
| `orchestrator` | Orchestrator mode |
| `ask` | Ask questions |
| `kickoff` | Session kickoff |

## How It Works

1. User runs `/snippet verify` → extension adds to `activeSnippets`.
2. Before every agent turn, `before_agent_start` hook prepends active snippet bodies to the system prompt.
3. Agent follows the behavioral contract automatically every turn.
4. User runs `/snippet off` → clears active snippets, injection stops.

Multiple snippets stack: `/snippet verify diagnose` → both injected every turn.

## Adding Snippets

When user adds a snippet:
1. Update `~/.omp/agent/snippets.json` with the new entry.
2. Confirm and show it in the list format.

JSON format:
```json
{
  "my-snippet": {
    "description": "One-line description",
    "body": "The behavioral contract text injected into system prompt."
  }
}
```

## Notes

- Snippets persist across turns until `/snippet off`.
- When snippets conflict, the stricter rule wins.
- Extension handles everything: CRUD, activation, and injection.
- Skill provides behavioral context for the agent when extension is unavailable.
