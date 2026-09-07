#!/usr/bin/env python3
"""Grok HQ — local mission control for builders using Grok / xAI.

Python 3 stdlib only. No secrets, no telemetry. Your data stays on this machine.

Commands:
  install / uninstall   write or remove ~/.grok-hq policy, agents, skills, store
  usage [--days N]      estimate/display xAI API usage from local logs (honest empty state)
  sessions [N]          list recent sessions from the local JSONL store
  search "query" [-n N] full-text search local conversation store
  show <session-id>     replay a session as readable text
  compile [--force]     compile sessions into dense digest cards
  brief [--days N]      compiled briefing of recent work
  serve [--port N]      local web dashboard (127.0.0.1)
  autopilot [off|tick]  Linux-friendly background recompile (cron/systemd/loop)
  version               print version
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

VERSION = "0.1.0"
HOME = os.path.expanduser("~")
HQ_DIR = os.path.dirname(os.path.abspath(__file__))
GROK_HQ = os.path.join(HOME, ".grok-hq")
SESSIONS_DIR = os.path.join(GROK_HQ, "sessions")
DIGEST_DIR = os.path.join(GROK_HQ, "digests")
USAGE_DIR = os.path.join(GROK_HQ, "usage")
AGENTS_DST = os.path.join(GROK_HQ, "agents")
SKILLS_DST = os.path.join(GROK_HQ, "skills")
POLICY_DST = os.path.join(GROK_HQ, "POLICY.md")
CONFIG_PATH = os.path.join(GROK_HQ, "config.json")
AUTOPILOT_PID = os.path.join(GROK_HQ, "autopilot.pid")
AUTOPILOT_LOG = os.path.join(GROK_HQ, "autopilot.log")
DEMO_SESSIONS = os.path.join(HQ_DIR, "demo", "sessions")

# Approximate public xAI list prices ($/MTok: input, output). Not billing truth —
# use console.x.ai for authoritative spend. Updated as of project seed; verify.
MODEL_RATES = {
    "grok-4": (3.0, 15.0),
    "grok-3": (3.0, 15.0),
    "grok-3-mini": (0.30, 0.50),
    "grok-2": (2.0, 10.0),
    "grok-2-vision": (2.0, 10.0),
}
DEFAULT_RATE = (3.0, 15.0)
STOPWORDS = set(
    "the a an and or of to in for on with is are was be this that it as at by from i you we".split()
)

# --------------------------------------------------------------------------- paths / IO


def ensure_store():
    for d in (GROK_HQ, SESSIONS_DIR, DIGEST_DIR, USAGE_DIR, AGENTS_DST,
              os.path.join(SKILLS_DST, "recall")):
        os.makedirs(d, exist_ok=True)


def parse_ts(ts):
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def short_model(model):
    m = (model or "unknown").lower()
    for key in sorted(MODEL_RATES, key=len, reverse=True):
        if key in m:
            return key
    if "grok" in m:
        return m.split("/")[-1][:24]
    return model or "unknown"


def entry_cost(model, u):
    if not isinstance(u, dict):
        return 0.0
    sm = short_model(model)
    r = MODEL_RATES.get(sm, DEFAULT_RATE)
    inp = u.get("input_tokens") or u.get("prompt_tokens") or 0
    out = u.get("output_tokens") or u.get("completion_tokens") or 0
    return (inp * r[0] + out * r[1]) / 1_000_000


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif "text" in b:
                    parts.append(str(b["text"]))
        return "\n".join(parts)
    if isinstance(content, dict) and "text" in content:
        return str(content["text"])
    return ""


def session_paths():
    """Yield (path, mtime) for local JSONL sessions; fall back to demo if empty."""
    seen = set()
    roots = [SESSIONS_DIR]
    live = glob.glob(os.path.join(SESSIONS_DIR, "**", "*.jsonl"), recursive=True)
    if not live and os.path.isdir(DEMO_SESSIONS):
        roots.append(DEMO_SESSIONS)
    for root in roots:
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            rp = os.path.realpath(path)
            if rp in seen:
                continue
            seen.add(rp)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            except OSError:
                continue
            yield path, mtime


def project_of(path):
    """Infer a short project label from path or embedded meta."""
    base = os.path.basename(os.path.dirname(path))
    if base in ("sessions", "demo"):
        return "local"
    return base or "local"


def iter_messages(path):
    """Adapter: local JSONL session store.

    Expected line shape (flexible):
      {"role":"user"|"assistant"|"system", "text"|"content":..., "ts"|"timestamp":...,
       "model":..., "usage":{...}, "project":...}
    """
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(d, dict):
                    continue
                # nested message wrappers (Claude-like) also accepted
                msg = d.get("message") if isinstance(d.get("message"), dict) else d
                role = (msg.get("role") or d.get("role") or "").lower()
                if role not in ("user", "assistant", "system", "tool"):
                    continue
                txt = text_of(msg.get("text") if msg.get("text") is not None else msg.get("content"))
                if not str(txt).strip():
                    continue
                yield {
                    "role": role,
                    "text": str(txt),
                    "ts": msg.get("ts") or msg.get("timestamp") or d.get("ts") or d.get("timestamp"),
                    "model": msg.get("model") or d.get("model"),
                    "usage": msg.get("usage") or d.get("usage"),
                    "project": msg.get("project") or d.get("project"),
                    "uuid": d.get("id") or d.get("uuid"),
                }
    except OSError:
        return


# --------------------------------------------------------------------------- adapters (stubs)


def adapter_notes():
    return """
Adapters (design notes — local JSONL is implemented; others are stubs):

  [done]  local JSONL   ~/.grok-hq/sessions/*.jsonl
          Tools/agents append one JSON object per turn. See demo/sessions/.

  [TODO]  grok.com web export
          No public local transcript path. If xAI adds export, drop JSON/JSONL
          into ~/.grok-hq/sessions/imports/ and re-run `hq compile`.

  [TODO]  Cursor / IDE Grok surface
          Point a watcher at the IDE's conversation store when the on-disk
          format is documented; normalize into the same JSONL schema.

  [TODO]  xAI API response logger
          Wrap your client to append usage+messages to ~/.grok-hq/usage/ and
          ~/.grok-hq/sessions/ on each call. Do NOT commit API keys.
""".strip()


# --------------------------------------------------------------------------- usage


def iter_usage_logs(days=None):
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for path in glob.glob(os.path.join(USAGE_DIR, "**", "*.jsonl"), recursive=True):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        except OSError:
            continue
        if cutoff and mtime < cutoff:
            # still open — lines may be older; filter per-line below
            pass
        yield path


def collect_usage(days):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    agg = {
        "by_model": defaultdict(lambda: defaultdict(float)),
        "by_project": defaultdict(float),
        "by_day": defaultdict(float),
        "by_day_model": defaultdict(lambda: defaultdict(float)),
        "win24h": defaultdict(float),
        "win7d": defaultdict(float),
        "total": 0.0,
        "msgs": 0,
        "now": now,
        "sources": [],
        "has_api_key": bool(os.environ.get("XAI_API_KEY")),
    }

    # 1) dedicated usage logs
    for path in iter_usage_logs(days):
        agg["sources"].append(path)
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(d, dict):
                        continue
                    t = parse_ts(d.get("ts") or d.get("timestamp"))
                    if t and t < cutoff:
                        continue
                    if not t:
                        t = now
                    model = d.get("model") or "unknown"
                    u = d.get("usage") or {
                        "input_tokens": d.get("input_tokens", 0),
                        "output_tokens": d.get("output_tokens", 0),
                    }
                    c = entry_cost(model, u)
                    _accumulate(agg, short_model(model), d.get("project") or "local", t, u, c, now)
        except OSError:
            continue

    # 2) usage embedded in session transcripts
    for path, _ in session_paths():
        proj = project_of(path)
        for m in iter_messages(path):
            if m["role"] != "assistant" or not m.get("usage"):
                continue
            t = parse_ts(m["ts"]) or now
            if t < cutoff:
                continue
            sm = short_model(m["model"])
            c = entry_cost(m["model"], m["usage"])
            _accumulate(agg, sm, m.get("project") or proj, t, m["usage"], c, now)

    return agg


def _accumulate(agg, sm, proj, t, u, c, now):
    b = agg["by_model"][sm]
    b["in"] += u.get("input_tokens") or u.get("prompt_tokens") or 0
    b["out"] += u.get("output_tokens") or u.get("completion_tokens") or 0
    b["cost"] += c
    b["msgs"] += 1
    agg["by_project"][proj] += c
    day = t.astimezone().strftime("%Y-%m-%d")
    agg["by_day"][day] += c
    agg["by_day_model"][day][sm] += c
    if t >= now - timedelta(hours=24):
        agg["win24h"][sm] += c
    if t >= now - timedelta(days=7):
        agg["win7d"][sm] += c
    agg["total"] += c
    agg["msgs"] += 1


def cmd_usage(days=14):
    a = collect_usage(days)
    print(f"Grok HQ · usage estimate, last {days} days ({a['msgs']} logged assistant turns)")
    print()
    if a["has_api_key"]:
        print("  XAI_API_KEY is set in this environment.")
        print("  Note: the key alone does not expose live spend — xAI billing lives in the")
        print("  console. This tool only aggregates LOCAL usage logs + session embeds.")
    else:
        print("  XAI_API_KEY not set. Usage below is from local logs only (if any).")
    print()
    if a["msgs"] == 0 and not a["sources"]:
        print("  (empty) No local usage yet.")
        print(f"  Write JSONL lines to {USAGE_DIR}/  e.g.:")
        print('    {"ts":"2026-09-07T12:00:00Z","model":"grok-3","usage":{"input_tokens":1000,"output_tokens":200},"project":"demo"}')
        print("  Or embed \"usage\" on assistant turns in ~/.grok-hq/sessions/*.jsonl")
        print("  Authoritative spend: https://console.x.ai — this is an estimate, not a bill.")
        return a
    w24, w7 = sum(a["win24h"].values()), sum(a["win7d"].values())
    print(f"  last 24 hours : ${w24:8.2f}   " + " ".join(
        f"{m}=${v:.2f}" for m, v in sorted(a["win24h"].items(), key=lambda x: -x[1])))
    print(f"  last 7 days   : ${w7:8.2f}   " + " ".join(
        f"{m}=${v:.2f}" for m, v in sorted(a["win7d"].items(), key=lambda x: -x[1])))
    print(f"  total {days:>2}d     : ${a['total']:8.2f}")
    if a["by_model"]:
        print("\n  by model:")
        for m, b in sorted(a["by_model"].items(), key=lambda x: -x[1]["cost"]):
            print(f"    {m:<16} ${b['cost']:9.2f}  in={b['in']/1e3:.1f}k  out={b['out']/1e3:.1f}k  turns={int(b['msgs'])}")
    if a["by_project"]:
        print("\n  by project:")
        for p, c in sorted(a["by_project"].items(), key=lambda x: -x[1])[:10]:
            print(f"    {p:<40} ${c:8.2f}")
    print("\n  Rates are approximate public list prices. Not a substitute for console.x.ai.")
    return a


# --------------------------------------------------------------------------- sessions / search / show


def session_meta(path):
    for m in iter_messages(path):
        if m["role"] == "user":
            return m["text"]
    return None


def session_project(path):
    for m in iter_messages(path):
        if m.get("project"):
            return m["project"]
    return project_of(path)


def cmd_sessions(limit=20):
    items = sorted(session_paths(), key=lambda x: -x[1].timestamp())[:limit]
    print(f"Grok HQ · {len(items)} most recent sessions\n")
    if not items:
        print(f"  (empty) Drop JSONL sessions into {SESSIONS_DIR}/")
        print(f"  Demo samples live in {DEMO_SESSIONS}/ — run `hq install` to seed the store.")
        return
    for path, mtime in items:
        sid = os.path.splitext(os.path.basename(path))[0]
        first = (session_meta(path) or "(no user text)").replace("\n", " ")[:90]
        print(f"  {mtime.astimezone().strftime('%m-%d %H:%M')}  {session_project(path):<24.24} {sid[:12]:<12}  {first}")
    print("\n  replay: hq show <session-id-prefix>")


def cmd_search(query, limit=8):
    terms = [t for t in re.findall(r"\w+", query.lower()) if t not in STOPWORDS]
    if not terms:
        print("Query too generic.")
        return
    hits = []
    for path, mtime in session_paths():
        sid = os.path.splitext(os.path.basename(path))[0]
        proj = session_project(path)
        for m in iter_messages(path):
            low = m["text"].lower()
            score = sum(low.count(t) for t in terms)
            if score == 0 or terms[0] not in low:
                continue
            idx = min((low.find(t) for t in terms if t in low), default=0)
            snippet = re.sub(r"\s+", " ", m["text"][max(0, idx - 80):idx + 200]).strip()
            hits.append((score, mtime, sid, proj, m["role"], snippet))
    hits.sort(key=lambda h: (-h[0], -h[1].timestamp()))
    if not hits:
        print(f"No matches for: {query}")
        print(f"(searched {SESSIONS_DIR} and demo sessions)")
        return
    print(f'Grok HQ · top {min(limit, len(hits))} of {len(hits)} matches for "{query}"\n')
    shown, seen = 0, set()
    for score, mtime, sid, proj, role, snippet in hits:
        if shown >= limit:
            break
        key = (sid, snippet[:40])
        if key in seen:
            continue
        seen.add(key)
        shown += 1
        print(f"  [{mtime.astimezone().strftime('%m-%d')}] {proj} · {sid[:12]} · {role}")
        print(f"     …{snippet}…\n")
    print("  replay: hq show <session-id-prefix>")


def cmd_show(sid_prefix, max_chars=40000):
    for path, _ in session_paths():
        if os.path.basename(path).startswith(sid_prefix):
            total = 0
            for m in iter_messages(path):
                t = parse_ts(m["ts"])
                stamp = t.astimezone().strftime("%H:%M") if t else "--:--"
                block = f"\n[{stamp}] {m['role'].upper()}:\n{m['text'].strip()}\n"
                total += len(block)
                if total > max_chars:
                    print("\n… (truncated)")
                    return
                print(block, end="")
            return
    print(f"No session starting with '{sid_prefix}'. Try: hq sessions")


# --------------------------------------------------------------------------- compile / brief


def compile_session(path):
    sid = os.path.splitext(os.path.basename(path))[0]
    proj = project_of(path)
    first_user = last_asst = None
    models = set()
    start_ts = end_ts = None
    cost = 0.0
    n_msgs = 0
    for m in iter_messages(path):
        n_msgs += 1
        t = parse_ts(m["ts"])
        if t:
            start_ts = start_ts or t
            end_ts = t
        if m["role"] == "user" and first_user is None:
            first_user = m["text"]
            if m.get("project"):
                proj = m["project"]
        if m["role"] == "assistant":
            if m.get("model"):
                models.add(short_model(m["model"]))
            if m.get("usage"):
                cost += entry_cost(m["model"], m["usage"])
            if len(m["text"]) > 40:
                last_asst = m["text"]
    if not first_user:
        return None
    day = (start_ts or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d")
    card = [
        f"# {sid}",
        f"date: {day} · project: {proj} · models: {','.join(sorted(models)) or '-'} · "
        f"est ${cost:.2f} · msgs {n_msgs}",
        f"\n## request\n{first_user[:500].strip()}",
    ]
    if last_asst:
        card.append(f"\n## outcome (final assistant message)\n{last_asst[:900].strip()}")
    return sid, day, proj, first_user, "\n".join(card) + "\n"


def cmd_compile(force=False, quiet=False):
    ensure_store()
    done = skipped = 0
    index = []
    for path, mtime in sorted(session_paths(), key=lambda x: x[1].timestamp()):
        sid = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(DIGEST_DIR, sid + ".md")
        fresh = os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(path)
        if fresh and not force:
            skipped += 1
        else:
            r = compile_session(path)
            if not r:
                continue
            with open(out, "w") as f:
                f.write(r[4])
            done += 1
        try:
            with open(out) as f:
                head = f.read(400).splitlines()
            req = next((l for l in head if l and not l.startswith(("#", "date:"))), "")[:80]
            meta = next((l for l in head if l.startswith("date:")), "")
            day = meta[6:16] if len(meta) >= 16 else "????-??-??"
            proj = meta.split("project: ")[-1].split(" ·")[0] if "project: " in meta else "?"
            index.append((day, f"- {day} · {sid[:12]} · {proj:<24.24} {req}"))
        except (OSError, StopIteration):
            continue
    index.sort(reverse=True)
    idx_path = os.path.join(GROK_HQ, "INDEX.md")
    with open(idx_path, "w") as f:
        f.write("# Grok HQ · compiled session index (newest first)\n\n"
                + "\n".join(l for _, l in index) + "\n")
    if not quiet:
        print(f"Compiled {done} sessions ({skipped} already fresh) → {DIGEST_DIR}")
        print(f"Index: {idx_path}")


def cmd_brief(days=14, max_sessions=15):
    ensure_store()
    cmd_compile(quiet=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).astimezone().strftime("%Y-%m-%d")
    cards = []
    for f in glob.glob(os.path.join(DIGEST_DIR, "*.md")):
        with open(f) as fh:
            txt = fh.read()
        m = re.search(r"date: (\S+) · project: (.+?) ·", txt)
        if not m or m.group(1) < cutoff:
            continue
        req = re.search(r"## request\n(.+)", txt)
        out = re.search(r"## outcome.*?\n(.+)", txt)
        cards.append((
            m.group(1), m.group(2),
            re.sub(r"\s+", " ", req.group(1))[:110] if req else "",
            re.sub(r"\s+", " ", out.group(1))[:130] if out else "",
        ))
    cards.sort(reverse=True)
    lines = [
        f"# Grok HQ briefing (last {days}d, {min(len(cards), max_sessions)} of {len(cards)} sessions)"
    ]
    if not cards:
        lines.append("\n_(no digests yet — add sessions under ~/.grok-hq/sessions/ then `hq compile`)_")
    for day, proj, req, out in cards[:max_sessions]:
        lines.append(f"\n**{day} · {proj}**")
        lines.append(f"- asked: {req}")
        if out:
            lines.append(f"- outcome: {out}")
    lines.append(
        f"\n_Digests: ~/.grok-hq/digests/ · search: `hq search \"terms\"` · replay: `hq show <id>`_"
    )
    print("\n".join(lines))


# --------------------------------------------------------------------------- install / uninstall


def seed_demo_sessions():
    """Copy demo JSONL into the live store if the store is empty."""
    ensure_store()
    n = 0
    existing = glob.glob(os.path.join(SESSIONS_DIR, "*.jsonl"))
    if not existing and os.path.isdir(DEMO_SESSIONS):
        for src in glob.glob(os.path.join(DEMO_SESSIONS, "*.jsonl")):
            dst = os.path.join(SESSIONS_DIR, os.path.basename(src))
            shutil.copy2(src, dst)
            n += 1
    demo_usage = os.path.join(HQ_DIR, "demo", "usage")
    if not glob.glob(os.path.join(USAGE_DIR, "*.jsonl")) and os.path.isdir(demo_usage):
        for src in glob.glob(os.path.join(demo_usage, "*.jsonl")):
            shutil.copy2(src, os.path.join(USAGE_DIR, os.path.basename(src)))
            n += 1
    return n


def cmd_install():
    ensure_store()
    # agents
    agents_src = os.path.join(HQ_DIR, "agents")
    for f in sorted(os.listdir(agents_src)):
        if f.endswith(".md"):
            shutil.copy2(os.path.join(agents_src, f), os.path.join(AGENTS_DST, f))
            print(f"  agent installed: ~/.grok-hq/agents/{f}")
    # recall skill
    skill_src = os.path.join(HQ_DIR, "skills", "recall", "SKILL.md")
    skill_dst = os.path.join(SKILLS_DST, "recall", "SKILL.md")
    os.makedirs(os.path.dirname(skill_dst), exist_ok=True)
    with open(skill_src) as s:
        content = s.read().replace("{{HQ_PATH}}", os.path.join(HQ_DIR, "hq.py"))
    with open(skill_dst, "w") as d:
        d.write(content)
    print("  skill installed:  ~/.grok-hq/skills/recall")
    # policy
    shutil.copy2(os.path.join(HQ_DIR, "POLICY.md"), POLICY_DST)
    print("  policy installed: ~/.grok-hq/POLICY.md")
    # config
    cfg = {
        "version": VERSION,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "hq_path": HQ_DIR,
        "sessions_dir": SESSIONS_DIR,
        "usage_dir": USAGE_DIR,
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print(f"  config:          ~/.grok-hq/config.json")
    # adapters note
    with open(os.path.join(GROK_HQ, "ADAPTERS.md"), "w") as f:
        f.write(adapter_notes() + "\n")
    print("  adapters note:   ~/.grok-hq/ADAPTERS.md")
    n = seed_demo_sessions()
    if n:
        print(f"  seeded {n} demo file(s) into ~/.grok-hq/ (sessions/usage)")
    else:
        print(f"  sessions store:  ~/.grok-hq/sessions/ (ready)")
    print("\nDone. Alias tip:")
    print(f"  alias hq='python3 {os.path.join(HQ_DIR, 'hq.py')}'")
    print("\nUninstall: python3 hq.py uninstall")


def cmd_uninstall():
    if not os.path.isdir(GROK_HQ):
        print("~/.grok-hq not found — nothing to uninstall.")
        return
    print("Grok HQ uninstall removes optional policy/agents/skills and config.")
    print("Session digests and JSONL logs are KEPT (your data).")
    print()
    removed = []
    for rel in ("agents", "skills", "POLICY.md", "config.json", "ADAPTERS.md",
                "autopilot.pid", "autopilot.log", "autopilot.sh"):
        path = os.path.join(GROK_HQ, rel)
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed.append(rel + "/")
        elif os.path.isfile(path):
            os.remove(path)
            removed.append(rel)
    # stop loop if running
    if os.path.exists(AUTOPILOT_PID):
        try:
            with open(AUTOPILOT_PID) as f:
                pid = int(f.read().strip())
            os.kill(pid, 15)
        except (OSError, ValueError):
            pass
    if removed:
        print("  removed:", ", ".join(removed))
    print(f"  kept: ~/.grok-hq/sessions, digests, usage, INDEX.md")
    print("  To wipe everything:  rm -rf ~/.grok-hq")


# --------------------------------------------------------------------------- serve / autopilot


def cmd_serve(port=8765):
    server = os.path.join(HQ_DIR, "hqserver.py")
    os.execv(sys.executable, [sys.executable, server, "--port", str(port)])


def write_autopilot_script():
    script = os.path.join(GROK_HQ, "autopilot.sh")
    hq = os.path.join(HQ_DIR, "hq.py")
    body = f"""#!/bin/bash
# Grok HQ autopilot loop — recompile digests every 10 minutes.
# Installed by: python3 hq.py autopilot
HQ="{hq}"
LOG="{AUTOPILOT_LOG}"
echo $$ > "{AUTOPILOT_PID}"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) autopilot start pid=$$" >> "$LOG"
while true; do
  python3 "$HQ" autopilot tick >> "$LOG" 2>&1
  sleep 600
done
"""
    with open(script, "w") as f:
        f.write(body)
    os.chmod(script, 0o755)
    return script


def cmd_autopilot(action="on"):
    """Linux-first autopilot: loop script + cron/systemd notes. macOS launchd note too."""
    ensure_store()
    if action == "tick":
        cmd_compile(quiet=True)
        stamp = os.path.join(GROK_HQ, ".last-tick")
        open(stamp, "w").close()
        return

    if action == "off":
        if os.path.exists(AUTOPILOT_PID):
            try:
                with open(AUTOPILOT_PID) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 15)
                print(f"Sent SIGTERM to autopilot pid {pid}")
            except (OSError, ValueError) as e:
                print(f"Could not stop loop ({e}); remove cron/systemd unit if you used those.")
            try:
                os.remove(AUTOPILOT_PID)
            except OSError:
                pass
        print("Autopilot off (loop stopped if we started it). Cron/systemd units are manual.")
        return

    # default: enable / document
    script = write_autopilot_script()
    hq = os.path.join(HQ_DIR, "hq.py")
    print("Grok HQ autopilot")
    print()
    print("  Option A — simple background loop (Linux/macOS):")
    print(f"    nohup {script} >/dev/null 2>&1 &")
    print(f"    # pid file: {AUTOPILOT_PID}  log: {AUTOPILOT_LOG}")
    print()
    print("  Option B — cron (every 10 min):")
    print(f"    */10 * * * * {sys.executable} {hq} autopilot tick")
    print()
    print("  Option C — systemd user timer (sketch):")
    print("    ~/.config/systemd/user/grok-hq-autopilot.service")
    print(f"      ExecStart={sys.executable} {hq} autopilot tick")
    print("    ~/.config/systemd/user/grok-hq-autopilot.timer")
    print("      OnUnitActiveSec=10min")
    print("    systemctl --user enable --now grok-hq-autopilot.timer")
    print()
    print("  Option D — macOS launchd: StartInterval 600 calling the same tick command.")
    print()
    # start the loop if on Linux and not already running
    running = False
    if os.path.exists(AUTOPILOT_PID):
        try:
            with open(AUTOPILOT_PID) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            running = True
            print(f"  Loop already running (pid {pid}).")
        except (OSError, ValueError):
            running = False
    if not running and sys.platform.startswith("linux"):
        # start detached
        import subprocess
        subprocess.Popen(
            ["bash", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.3)
        print(f"  Started loop via {script}")
        print("  Digests recompile every 10 minutes. Stop with: hq autopilot off")
    elif not running:
        print("  (non-Linux) Start Option A/B/D yourself — stub ready.")


def cmd_version():
    print(f"Grok HQ {VERSION}")
    print(f"  hq:      {HQ_DIR}")
    print(f"  store:   {GROK_HQ}")
    print(f"  python:  {sys.version.split()[0]}")


# --------------------------------------------------------------------------- main


def build_parser():
    p = argparse.ArgumentParser(
        prog="hq",
        description="Grok HQ — mission control for builders using Grok / xAI",
    )
    p.add_argument("--version", action="store_true", help="print version and exit")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("install", help="install policy/agents/skills under ~/.grok-hq/")
    sub.add_parser("uninstall", help="remove installed policy/agents/skills (keeps sessions)")
    pu = sub.add_parser("usage", help="show estimated xAI usage from local logs")
    pu.add_argument("--days", type=int, default=14)
    ps = sub.add_parser("sessions", help="list recent local sessions")
    ps.add_argument("limit", nargs="?", type=int, default=20)
    pse = sub.add_parser("search", help="full-text search local sessions")
    pse.add_argument("query", nargs="+")
    pse.add_argument("-n", type=int, default=8)
    psh = sub.add_parser("show", help="replay a session")
    psh.add_argument("session_id")
    pc = sub.add_parser("compile", help="compile sessions into digest cards")
    pc.add_argument("--force", action="store_true")
    pb = sub.add_parser("brief", help="print compiled work briefing")
    pb.add_argument("--days", type=int, default=14)
    pv = sub.add_parser("serve", help="launch local web dashboard")
    pv.add_argument("--port", type=int, default=8765)
    pa = sub.add_parser("autopilot", help="background digest recompile (Linux-friendly)")
    pa.add_argument("action", nargs="?", default="on", choices=["on", "off", "tick"])
    sub.add_parser("version", help="print version")
    return p


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if args.version or args.cmd == "version":
        cmd_version()
        return 0
    if args.cmd == "install":
        cmd_install()
    elif args.cmd == "uninstall":
        cmd_uninstall()
    elif args.cmd == "usage":
        cmd_usage(args.days)
    elif args.cmd == "sessions":
        cmd_sessions(args.limit)
    elif args.cmd == "search":
        cmd_search(" ".join(args.query), limit=args.n)
    elif args.cmd == "show":
        cmd_show(args.session_id)
    elif args.cmd == "compile":
        cmd_compile(force=args.force)
    elif args.cmd == "brief":
        cmd_brief(days=args.days)
    elif args.cmd == "serve":
        cmd_serve(args.port)
    elif args.cmd == "autopilot":
        cmd_autopilot(args.action)
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
