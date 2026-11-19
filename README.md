# Grok HQ

**Mission control for builders using Grok / xAI.** Open-source counterpart to [Claude HQ](https://github.com/mohanadbarakattt/claude-hq).

One stdlib Python CLI + a local dashboard. Know your burn (when you log it), search past sessions, compile digests into a briefing, install a small agent doctrine tree under `~/.grok-hq/`. No telemetry. No secrets in the repo. Your data stays on the machine.

## Install (30 seconds)

```bash
git clone https://github.com/mohanadbarakattt/grok-hq && cd grok-hq
python3 hq.py install
alias hq='python3 $(pwd)/hq.py'   # add to your shell rc
hq serve                          # → http://127.0.0.1:8765
```

`install` writes inspectable, reversible files under `~/.grok-hq/`:

- `agents/{executor,grunt,reviewer}.md` — delegate / verify doctrine
- `skills/recall/SKILL.md` — how agents run `hq search`
- `POLICY.md` — builder policy block
- empty (or demo-seeded) `sessions/`, `usage/`, `digests/`

## Keep all your AI tools on the same page (`hq sync`)

Grokbot, Claude, ChatGPT/Codex and Cursor each keep their own memory. `hq sync` connects them:

- **One shared memory file:** `~/.grok-hq/hub/MEMORY.md`. Write who you are, your projects and your decisions once. Every tool is told to append new durable facts under `## Inbox`, so what one AI learns, the others see.
- **Pull:** imports history from the Grok Bot desktop app (`%APPDATA%\Grok Bot`, auto-detected), Claude Code (`~/.claude/projects`), Codex (`~/.codex/sessions`) and any folder you point it at into `~/.grok-hq/sessions/<tool>/`. Searchable with `hq search`.
- **Push:** writes one context block (shared memory + recent work from *all* tools) into:
  - `~/.claude/CLAUDE.md` (Claude Code, all projects)
  - `~/.codex/AGENTS.md` (Codex)
  - any file you list, such as Grokbot's memory/rules file
  - each project you add: `AGENTS.md` (Codex/Cursor/Grok), `CLAUDE.md`, `.cursor/rules/grok-hq.mdc`
  - paste-ready text for web apps (ChatGPT custom instructions, claude.ai project, grok.com) in `~/.grok-hq/hub/paste/`

  The block sits between `<!-- grok-hq:begin -->` / `<!-- grok-hq:end -->`. Your own text is never touched, and the first edit of any existing file is backed up to `~/.grok-hq/backups/`.

```powershell
python hq.py install
python hq.py sync --status                          # what's detected
python hq.py sync --probe-grokbot                   # Grok Bot storage format (no chat text shown)
notepad $HOME\.grok-hq\sync.json                   # set Grokbot history folder + memory file
python hq.py sync --add-project "C:\code\myapp"    # optional, per project
python hq.py sync --dry-run                         # preview
python hq.py sync                                   # do it
python hq.py autopilot                              # Windows Task Scheduler: sync every 10 min
python hq.py sync --remove                          # undo: strip all blocks
```

Cursor's own chat history lives in SQLite and isn't imported yet, but Cursor still *receives* the shared context through the project rule file.

## Commands

```bash
hq usage                 # local usage estimate (honest empty state)
hq sessions              # recent JSONL sessions
hq search "auth bug"     # full-text search
hq show demo-001         # replay a session
hq compile               # digest cards → ~/.grok-hq/digests/
hq brief                 # compiled briefing
hq serve                 # local web panel
hq sync                  # pull history from all AIs + push shared context to all
hq memory                # show the shared memory file
hq autopilot             # sync every 10 min (Windows Task Scheduler / Linux loop)
hq uninstall             # remove agents/skills/policy (keeps your sessions)
hq version
```

## Why it exists

Grok is useful. The tooling around it is still thin compared to Claude Code's local transcript world. Grok HQ gives you a **clear local contract**:

1. **Usage** — aggregate `~/.grok-hq/usage/*.jsonl` and usage embeds in sessions. If `XAI_API_KEY` is set we acknowledge it; we do **not** pretend the key reveals live billing. Authoritative spend: [console.x.ai](https://console.x.ai).
2. **Recall** — index/search `~/.grok-hq/sessions/*.jsonl`. Stub adapters for grok.com export / IDE stores are documented with TODOs.
3. **Compile / brief** — dense digest cards + a briefing you can paste into a new chat.
4. **Serve** — stdlib `http.server` + single `app.html` panel.
5. **Install** — optional policy/agents/skills under `~/.grok-hq/`.
6. **Autopilot** — recompile digests every 10 minutes (loop script on Linux; cron/systemd/launchd documented).

## Session format

One JSON object per line in `~/.grok-hq/sessions/<id>.jsonl`:

```json
{"role":"user","text":"Ship the rate-limit fix","ts":"2026-09-07T12:00:00Z","project":"radar"}
{"role":"assistant","text":"Done. Verified with …","ts":"2026-09-07T12:04:00Z","model":"grok-3","usage":{"input_tokens":1200,"output_tokens":400}}
```

Tools and agents can append freely. Demo samples ship under `demo/sessions/`.

## Uninstall

```bash
python3 hq.py uninstall          # removes agents/skills/policy/config
rm -rf ~/.grok-hq                # nuclear — also deletes sessions/digests
```

## License

MIT © 2026 Mohanad Barakat
