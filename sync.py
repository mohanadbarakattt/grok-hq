"""Grok HQ sync — keep every AI tool on the same page.

PULL  : import conversation history from each AI tool into ~/.grok-hq/sessions/<tool>/
PUSH  : write one shared context block (memory + recent work across ALL tools) into each
        tool's own memory / rules file, between markers, never touching your own text.

Shared memory lives in ONE file: ~/.grok-hq/hub/MEMORY.md. Every tool is told to append
durable facts there, so what one AI learns, the others see on the next sync.

Python 3 stdlib only. Works on Windows, macOS and Linux.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import base64
import gzip
import zlib
from datetime import datetime, timezone

BEGIN = "<!-- grok-hq:begin -->"
END = "<!-- grok-hq:end -->"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)

MEMORY_TEMPLATE = """# Shared AI memory

One file, read by every AI tool I use (Grokbot, Claude, ChatGPT/Codex, Cursor).
Edit freely. `hq sync` copies this into each tool's memory/rules file.

## About me
- (who you are, what you do, how you like answers — e.g. brief and to the point)

## Active projects
- (project — one line on what it is, stack, where it lives)

## Decisions & conventions
- (things every AI should keep doing the same way)

## Inbox
<!-- AI tools append durable facts here: `- YYYY-MM-DD [tool] fact` -->
"""

DEFAULT_CONFIG = {
    "_help": "Edit me. Paths may use ~. Run `hq sync --status` to check what is detected.",
    "pull": {
        "claude_code": True,
        "codex": True,
        "grokbot_app": True,
        "extra": [],
    },
    "push": {
        "claude_global": True,
        "codex_global": True,
        "extra_files": [
            {"name": "grokbot", "path": "",
             "_help": "Set path to Grokbot's memory/rules/instructions file."}
        ],
    },
    "projects": [],
    "recent_days": 7,
    "max_recent": 12,
    "max_block_chars": 12000,
}


# --------------------------------------------------------------------------- helpers


def _read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".grokhq.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _jsonl(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    yield d
    except OSError:
        return


def _slug(s, n=40):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")
    return (s or "local")[:n]


def _proj_from_cwd(cwd):
    if not cwd:
        return None
    return os.path.basename(str(cwd).rstrip("/\\").replace("\\", "/")) or None


_HQ = None  # the hq module, set by Hub()


class Hub:
    def __init__(self, hq):
        global _HQ
        _HQ = hq
        self.hq = hq  # the hq module (paths, parsers, compile helpers)
        self.root = os.path.join(hq.GROK_HQ, "hub")
        self.memory = os.path.join(self.root, "MEMORY.md")
        self.context = os.path.join(self.root, "CONTEXT.md")
        self.paste = os.path.join(self.root, "paste")
        self.backups = os.path.join(hq.GROK_HQ, "backups")
        self.config_path = os.path.join(hq.GROK_HQ, "sync.json")
        self.home = hq.HOME

    # ----------------------------------------------------------------- setup

    def ensure(self):
        os.makedirs(self.paste, exist_ok=True)
        if not os.path.exists(self.memory):
            _write(self.memory, MEMORY_TEMPLATE)
        if not os.path.exists(self.config_path):
            _write(self.config_path, json.dumps(DEFAULT_CONFIG, indent=2) + "\n")

    def config(self):
        self.ensure()
        try:
            cfg = json.loads(_read(self.config_path))
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"Bad {self.config_path}: {e}")
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        return merged

    def save_config(self, cfg):
        _write(self.config_path, json.dumps(cfg, indent=2) + "\n")

    def p(self, path):
        return os.path.abspath(os.path.expanduser(path)) if path else ""

    # ----------------------------------------------------------------- PULL

    def _claude_files(self):
        return glob.glob(os.path.join(self.home, ".claude", "projects", "*", "*.jsonl"))

    # ---- Grok Bot desktop app (Electron). Chats live in
    #      %APPDATA%\Grok Bot\sand-client-persistence\<base32 name>.blob
    #      where the decoded name ends in ".transcript.replicas.<conversation-id>".

    def grokbot_dir(self):
        cands = []
        if os.environ.get("APPDATA"):
            cands.append(os.path.join(os.environ["APPDATA"], "Grok Bot"))
        cands += [os.path.join(self.home, "AppData", "Roaming", "Grok Bot"),
                  os.path.join(self.home, "Library", "Application Support", "Grok Bot"),
                  os.path.join(self.home, ".config", "Grok Bot")]
        for c in cands:
            if os.path.isdir(c):
                return c
        return None

    @staticmethod
    def decode_blob_name(path):
        stem = os.path.basename(path)
        stem = stem[:-5] if stem.endswith(".blob") else stem
        try:
            s = stem.upper()
            return base64.b32decode(s + "=" * (-len(s) % 8)).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return ""

    def _grokbot_files(self):
        root = self.grokbot_dir()
        if not root:
            return []
        out = []
        for f in glob.glob(os.path.join(root, "sand-client-persistence", "*.blob")):
            if ".transcript.replicas." in self.decode_blob_name(f):
                out.append(f)
        return out

    @staticmethod
    def load_blob(path):
        """Return (kind, data). kind: json | jsonl | text | binary."""
        with open(path, "rb") as f:
            raw = f.read()
        for fn in (gzip.decompress, zlib.decompress,
                   lambda b: zlib.decompress(b, -15)):
            try:
                raw = fn(raw)
                break
            except (OSError, zlib.error, EOFError):
                continue
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "binary", raw
        txt = txt.lstrip("\ufeff")
        try:
            return "json", json.loads(txt)
        except json.JSONDecodeError:
            pass
        rows = []
        for line in txt.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        if rows:
            return "jsonl", rows
        return "text", txt

    ROLE_KEYS = ("role", "sender", "author", "speaker", "from", "type")
    TEXT_KEYS = ("text", "content", "message", "body", "markdown", "query", "response", "parts")
    ROLE_MAP = {"user": "user", "human": "user", "me": "user",
                "assistant": "assistant", "grok": "assistant", "bot": "assistant",
                "model": "assistant", "ai": "assistant", "agent": "assistant"}

    @classmethod
    def _msg_from(cls, d):
        role = None
        for k in cls.ROLE_KEYS:
            v = d.get(k)
            if isinstance(v, dict):
                v = v.get("role") or v.get("name")
            if isinstance(v, str) and v.lower() in cls.ROLE_MAP:
                role = cls.ROLE_MAP[v.lower()]
                break
        if not role:
            return None
        txt = ""
        for k in cls.TEXT_KEYS:
            if k in d:
                txt = _HQ.text_of(d[k]) if not isinstance(d[k], str) else d[k]
                if isinstance(d[k], list) and not txt:
                    txt = "\n".join(str(p) for p in d[k] if isinstance(p, str))
                if txt and txt.strip():
                    break
        if not txt or not str(txt).strip():
            return None
        ts = None
        for k in ("createdAt", "created_at", "timestamp", "ts", "createTime", "time", "date"):
            if d.get(k):
                ts = d[k]
                if isinstance(ts, (int, float)) and ts > 1e12:
                    ts = ts / 1000.0
                break
        return {"role": role, "text": str(txt).strip(), "ts": ts,
                "model": d.get("model") or d.get("modelName")}

    @classmethod
    def walk_messages(cls, data):
        found, seen = [], set()

        def walk(x, depth=0):
            if depth > 40:
                return
            if isinstance(x, dict):
                m = cls._msg_from(x)
                if m:
                    key = (m["role"], m["text"][:200])
                    if key not in seen:
                        seen.add(key)
                        found.append(m)
                    return
                for v in x.values():
                    walk(v, depth + 1)
            elif isinstance(x, list):
                for v in x:
                    walk(v, depth + 1)
            elif isinstance(x, str) and len(x) > 20 and x.lstrip()[:1] in "[{":
                try:
                    walk(json.loads(x), depth + 1)  # JSON stored as a string
                except json.JSONDecodeError:
                    pass

        walk(data)
        if found and all(m["ts"] for m in found):
            try:
                found.sort(key=lambda m: float(m["ts"]) if isinstance(m["ts"], (int, float))
                           else _HQ.parse_ts(m["ts"]).timestamp())
            except (TypeError, ValueError, AttributeError):
                pass
        return found

    @classmethod
    def parse_grokbot(cls, path):
        kind, data = cls.load_blob(path)
        if kind in ("binary", "text"):
            return
        for m in cls.walk_messages(data):
            yield None, m

    def probe_grokbot(self, log=print):
        """Describe Grok Bot storage WITHOUT printing any chat content."""
        root = self.grokbot_dir()
        log(f"Grok Bot app folder: {root or 'NOT FOUND'}")
        if not root:
            return
        files = glob.glob(os.path.join(root, "sand-client-persistence", "*.blob"))
        kinds = {}
        for f in files:
            n = self.decode_blob_name(f)
            k = n.split(".")[5] if n.count(".") > 5 else n[:40]
            kinds[k] = kinds.get(k, 0) + 1
        log(f"  persistence blobs: {len(files)}  by kind: {kinds}")
        tr = self._grokbot_files()
        log(f"  transcript blobs : {len(tr)}")
        for f in sorted(tr, key=os.path.getmtime, reverse=True)[:5]:
            with open(f, "rb") as fh:
                head = fh.read(16)
            kind, data = self.load_blob(f)
            shape = ""
            if kind == "json":
                shape = ("keys=" + ",".join(list(data.keys())[:12])) if isinstance(data, dict) \
                    else f"list[{len(data)}]"
            elif kind == "jsonl":
                shape = f"rows={len(data)}"
            msgs = self.walk_messages(data) if kind in ("json", "jsonl") else []
            roles = {}
            for m in msgs:
                roles[m["role"]] = roles.get(m["role"], 0) + 1
            log(f"   - {os.path.getsize(f):>9} B  head={head.hex()}  {kind:<6} {shape[:90]}  "
                f"messages={roles or 0}")
        log("\n  (no message text is shown — safe to paste this output)")

    def _codex_files(self):
        root = os.environ.get("CODEX_HOME") or os.path.join(self.home, ".codex")
        return glob.glob(os.path.join(root, "sessions", "**", "*.jsonl"), recursive=True)

    @staticmethod
    def parse_claude(path):
        proj = None
        for d in _jsonl(path):
            if d.get("type") not in ("user", "assistant") or d.get("isMeta"):
                continue
            proj = proj or _proj_from_cwd(d.get("cwd"))
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            role = msg.get("role") or d.get("type")
            txt = _HQ.text_of(msg.get("content")).strip()
            if not txt or txt.startswith(("<command-", "<local-command", "Caveat:")):
                continue
            yield proj, {"role": role, "text": txt, "ts": d.get("timestamp"),
                         "model": msg.get("model"), "usage": msg.get("usage")}

    @staticmethod
    def parse_codex(path):
        proj = None
        for d in _jsonl(path):
            pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            if d.get("type") in ("session_meta", "turn_context"):
                proj = proj or _proj_from_cwd(pl.get("cwd"))
                continue
            if pl.get("type") != "message" or pl.get("role") not in ("user", "assistant"):
                continue
            parts = []
            for c in pl.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    parts.append(c["text"])
            txt = "\n".join(parts).strip()
            if not txt or txt.startswith(("<environment_context", "<user_instructions",
                                          "# AGENTS.md")):
                continue
            yield proj, {"role": pl["role"], "text": txt, "ts": d.get("timestamp")}

    @staticmethod
    def parse_generic(path):
        for m in _HQ.iter_messages(path):
            yield m.get("project"), m

    def _import(self, source, path, parser, dry):
        dst_dir = os.path.join(self.hq.SESSIONS_DIR, source)
        stem = os.path.splitext(os.path.basename(path))[0]
        if source == "grokbot" and ".transcript.replicas." in self.decode_blob_name(path):
            stem = "grok-" + self.decode_blob_name(path).rsplit(".transcript.replicas.", 1)[1]
        if len(stem) > 80:  # keep ids unique when names share a long prefix
            import hashlib
            stem = stem[:60] + "-" + hashlib.sha1(stem.encode()).hexdigest()[:12]
        sid = _slug(stem, 80)
        # project folder is decided after parsing; look for an existing copy first
        existing = glob.glob(os.path.join(dst_dir, "*", sid + ".jsonl"))
        try:
            src_m = os.path.getmtime(path)
        except OSError:
            return "skip"
        if existing and os.path.getmtime(existing[0]) >= src_m:
            return "fresh"
        rows, proj = [], None
        for p, m in parser(path):
            proj = proj or p
            rows.append(m)
        if not any(r["role"] == "user" for r in rows):
            return "skip"
        proj = _slug(proj or "local")
        label = f"{proj} [{source}]"
        if dry:
            return "new"
        for e in existing:
            os.remove(e)
        out = os.path.join(dst_dir, proj, sid + ".jsonl")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                rec = {k: v for k, v in r.items() if v not in (None, "", {})}
                rec["project"] = label
                rec["source"] = source
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return "new"

    def pull(self, cfg, dry=False, log=print):
        jobs = []
        pc = cfg["pull"]
        if pc.get("claude_code"):
            jobs += [("claude", f, self.parse_claude) for f in self._claude_files()]
        if pc.get("grokbot_app"):
            jobs += [("grokbot", f, self.parse_grokbot) for f in self._grokbot_files()]
        if pc.get("codex"):
            jobs += [("codex", f, self.parse_codex) for f in self._codex_files()]
        for ex in pc.get("extra") or []:
            root = self.p(ex.get("path"))
            if not root or not os.path.isdir(root):
                continue
            pat = ex.get("glob") or "**/*.jsonl"
            files = glob.glob(os.path.join(root, pat), recursive=True)
            jobs += [(_slug(ex.get("name") or "extra"), f, self.parse_generic) for f in files]
        stats = {}
        for source, f, parser in jobs:
            r = self._import(source, f, parser, dry)
            s = stats.setdefault(source, {"new": 0, "fresh": 0, "skip": 0})
            s[r] += 1
        for source, s in sorted(stats.items()):
            log(f"  pull {source:<10} {s['new']} new/updated · {s['fresh']} unchanged · {s['skip']} empty")
        if not stats:
            log("  pull: no AI history found yet (see `hq sync --status`).")
        return stats

    # ----------------------------------------------------------------- context

    def build_block(self, cfg):
        hq = self.hq
        if not hasattr(hq, "brief_text"):
            raise RuntimeError("hq.brief_text missing")
        mem = _read(self.memory).strip()
        mem = re.sub(r"<!--.*?-->", "", mem, flags=re.S)
        mem = mem[mem.find("## "):] if "## " in mem else mem  # drop title + intro
        mem = re.sub(r"\n{3,}", "\n\n", mem).strip()
        mem = re.sub(r"^## ", "### ", mem, flags=re.M)
        recent = hq.brief_text(days=cfg["recent_days"], max_sessions=cfg["max_recent"],
                               header=False)
        hq_cmd = f'python "{os.path.join(hq.HQ_DIR, "hq.py")}"'
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        body = f"""{BEGIN}
# Shared AI context — synced {stamp} by grok-hq (do not edit inside this block)

I use several AI tools (Grokbot, Claude, ChatGPT/Codex, Cursor). This block keeps you all
on the same page. Treat it as background context, not as a task.

- Shared memory file: `{self.memory}`
  When you learn a durable fact, decision or preference, append ONE line under `## Inbox`
  there: `- YYYY-MM-DD [your tool name] fact`. Never delete other lines.
- Search past work from every tool: `{hq_cmd} search "terms"` · replay: `{hq_cmd} show <id>`

## Memory
{mem}

## Recent work across all tools
{recent.strip() or "_(nothing synced yet)_"}
{END}
"""
        limit = int(cfg.get("max_block_chars") or 12000)
        if len(body) > limit:
            cut = body[: limit - len(END) - 40].rsplit("\n", 1)[0]
            body = cut + "\n_(truncated — see hub/CONTEXT.md)_\n" + END + "\n"
        return body

    # ----------------------------------------------------------------- PUSH

    def targets(self, cfg):
        t = []
        ps = cfg["push"]
        if ps.get("claude_global"):
            t.append(("Claude Code (global)", os.path.join(self.home, ".claude", "CLAUDE.md"), "block"))
        if ps.get("codex_global"):
            root = os.environ.get("CODEX_HOME") or os.path.join(self.home, ".codex")
            t.append(("Codex (global)", os.path.join(root, "AGENTS.md"), "block"))
        for ex in ps.get("extra_files") or []:
            if ex.get("path"):
                t.append((ex.get("name") or "extra", self.p(ex["path"]), "block"))
        for proj in cfg.get("projects") or []:
            root = self.p(proj)
            if not os.path.isdir(root):
                continue
            name = os.path.basename(root)
            t.append((f"{name}: AGENTS.md (Codex/Cursor/Grok)", os.path.join(root, "AGENTS.md"), "block"))
            t.append((f"{name}: CLAUDE.md", os.path.join(root, "CLAUDE.md"), "block"))
            t.append((f"{name}: Cursor rule", os.path.join(root, ".cursor", "rules", "grok-hq.mdc"), "mdc"))
        return t

    def _backup_once(self, path):
        os.makedirs(self.backups, exist_ok=True)
        tag = re.sub(r"[^A-Za-z0-9]+", "_", path)[-120:]
        if glob.glob(os.path.join(self.backups, tag + ".*.bak")):
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, os.path.join(self.backups, f"{tag}.{stamp}.bak"))

    def apply_block(self, path, block, mode, dry=False):
        if mode == "mdc":
            new = ("---\ndescription: Shared context from grok-hq (all AI tools)\n"
                   "alwaysApply: true\n---\n" + block)
            old = _read(path) if os.path.exists(path) else None
        else:
            old = _read(path) if os.path.exists(path) else None
            if old is None:
                new = block
            elif BEGIN in old and END in old:
                new = BLOCK_RE.sub(lambda _m: block, old, count=1)
            else:
                new = old.rstrip("\n") + ("\n\n" if old.strip() else "") + block
        if old == new:
            return "unchanged"
        if dry:
            return "would update" if old is not None else "would create"
        if old is not None:
            self._backup_once(path)
        _write(path, new)
        return "updated" if old is not None else "created"

    def remove_block(self, path):
        if not os.path.exists(path):
            return "absent"
        if path.endswith("grok-hq.mdc"):
            os.remove(path)
            return "deleted"
        old = _read(path)
        new = BLOCK_RE.sub("", old).rstrip("\n") + "\n"
        if new == old:
            return "no block"
        if not new.strip():
            os.remove(path)
            return "deleted (was only the block)"
        _write(path, new)
        return "block removed"

    def write_paste_files(self, block):
        inner = BLOCK_RE.sub(lambda m: m.group(0)[len(BEGIN):-len(END) - 1], block).strip()
        inner = inner.replace(END, "").strip()
        _write(self.context, inner + "\n")
        mem = re.sub(r"<!--.*?-->", "", _read(self.memory), flags=re.S)
        mem = re.sub(r"\n{3,}", "\n\n", mem).strip()
        # ChatGPT custom-instructions fields cap at 1500 chars.
        short = ("I use several AI tools that share one memory file. Key context:\n\n"
                 + mem.replace("# Shared AI memory", "").strip())
        _write(os.path.join(self.paste, "chatgpt-custom-instructions.txt"), short[:1500].rstrip() + "\n")
        _write(os.path.join(self.paste, "claude-app-project-instructions.md"), inner + "\n")
        _write(os.path.join(self.paste, "grok-web-custom-instructions.txt"), short[:1500].rstrip() + "\n")

    def push(self, cfg, dry=False, log=print):
        block = self.build_block(cfg)
        if not dry:
            self.write_paste_files(block)
        for name, path, mode in self.targets(cfg):
            try:
                r = self.apply_block(path, block, mode, dry)
            except OSError as e:
                r = f"ERROR {e}"
            log(f"  push {r:<13} {name:<34} {path}")
        if not dry:
            log(f"  paste-ready  web apps (ChatGPT / claude.ai / grok.com) → {self.paste}")

    # ----------------------------------------------------------------- status

    def status(self, cfg, log=print):
        def mark(ok):
            return "found  " if ok else "missing"
        log("Grok HQ sync · status\n")
        log(f"  shared memory : {self.memory}")
        log(f"  config        : {self.config_path}\n")
        log("  PULL (history in)")
        log(f"    {mark(bool(self._claude_files()))} Claude Code   {len(self._claude_files())} transcripts in ~/.claude/projects")
        log(f"    {mark(bool(self._codex_files()))} Codex         {len(self._codex_files())} sessions in ~/.codex/sessions")
        for ex in cfg["pull"].get("extra") or []:
            path = self.p(ex.get("path"))
            ok = bool(path) and os.path.isdir(path)
            log(f"    {mark(ok)} {ex.get('name', 'extra'):<13} {path or '(path not set in sync.json)'}")
        gb = self._grokbot_files()
        log(f"    {mark(bool(gb))} Grok Bot      {len(gb)} chats in {self.grokbot_dir() or '%APPDATA%/Grok Bot (not found)'}")
        log("    n/a     Cursor chat   stored in SQLite; not imported (Cursor still receives context)")
        log("\n  PUSH (context out)")
        for name, path, _ in self.targets(cfg):
            has = os.path.exists(path) and BEGIN in _read(path)
            log(f"    {'synced ' if has else 'pending'} {name:<34} {path}")
        for ex in cfg["push"].get("extra_files") or []:
            if not ex.get("path"):
                log(f"    unset   {ex.get('name', 'extra'):<34} (set push.extra_files path in sync.json)")
        if not cfg.get("projects"):
            log("    tip     add project folders so Cursor/Codex/Claude see context there:")
            log('            hq sync --add-project "C:\\path\\to\\project"')
