---
name: recall
description: Search and replay past Grok HQ conversations on this machine — use when the user references earlier work ("as we discussed", "the bug from last week", "what did we decide about X"), when starting work a previous session may have touched, or whenever prior-session context would prevent re-doing or re-asking.
---

# Recall — cross-session memory

Local sessions live as JSONL under `~/.grok-hq/sessions/`. This skill lets you search them and replay any session via Grok HQ.

## Commands (run via shell)

```bash
python3 {{HQ_PATH}} search "<query terms>" -n 8    # ranked snippets
python3 {{HQ_PATH}} sessions 20                    # recent sessions
python3 {{HQ_PATH}} show <session-id-prefix>       # replay as plain text
python3 {{HQ_PATH}} brief                          # compiled recent-work briefing
```

## How to use it well

1. Search with 2–4 specific nouns from the user's reference (project names, function names, error text) — not full sentences.
2. Results show date, project, session id, and a snippet. If a snippet looks right, `show` that session id.
3. Prefer delegating big replays to the `grunt` agent ("run `hq show <id>` and summarize the decisions") to keep this context lean.
4. If nothing matches, broaden terms once, then tell the user what you searched. Never present a guess as a remembered fact.
5. Treat recalled content as historical: verify that files/decisions it mentions still hold before acting on them.

## Writing new sessions

Tools and wrappers can append turns to `~/.grok-hq/sessions/<id>.jsonl` (one JSON object per line). See the repo README for the schema. Other surfaces (grok.com export, IDE stores) are stub adapters — drop normalized JSONL into the sessions dir when available.
