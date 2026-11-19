#!/usr/bin/env python3
"""Grok HQ — local control-panel server (stdlib only, binds 127.0.0.1).

Run:  python3 hqserver.py [--port 8765]
  or: python3 hq.py serve
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import hq  # sibling module

HOME = os.path.expanduser("~")
GROK_HQ = hq.GROK_HQ
HQ_DIR = hq.HQ_DIR

ALLOWED_ROOTS = [
    os.path.join(GROK_HQ, "agents"),
    os.path.join(GROK_HQ, "skills"),
    os.path.join(GROK_HQ, "digests"),
    os.path.join(GROK_HQ, "POLICY.md"),
    os.path.join(GROK_HQ, "ADAPTERS.md"),
    os.path.join(GROK_HQ, "INDEX.md"),
    os.path.join(GROK_HQ, "config.json"),
]


def safe_path(p):
    rp = os.path.realpath(os.path.expanduser(p))
    return any(
        rp == os.path.realpath(r) or rp.startswith(os.path.realpath(r) + os.sep)
        for r in ALLOWED_ROOTS
    )


def api_overview(days):
    a = hq.collect_usage(days)
    return {
        "win24h": sum(a["win24h"].values()),
        "win7d": sum(a["win7d"].values()),
        "total": a["total"],
        "msgs": a["msgs"],
        "days": days,
        "has_api_key": a["has_api_key"],
        "win24h_detail": dict(a["win24h"]),
        "win7d_detail": dict(a["win7d"]),
        "by_model": {m: dict(b) for m, b in a["by_model"].items()},
        "by_day": [{"day": d, "models": dict(a["by_day_model"][d])} for d in sorted(a["by_day"])],
        "by_project": sorted(a["by_project"].items(), key=lambda x: -x[1])[:15],
    }


def api_sessions(n):
    items = sorted(hq.session_paths(), key=lambda x: -x[1].timestamp())[:n]
    out = []
    for path, mtime in items:
        sid = os.path.splitext(os.path.basename(path))[0]
        out.append({
            "sid": sid,
            "project": hq.session_project(path),
            "mtime": mtime.astimezone().strftime("%Y-%m-%d %H:%M"),
            "first": (hq.session_meta(path) or "")[:140],
        })
    return out


def api_search(q, n=12):
    terms = [t for t in re.findall(r"\w+", q.lower()) if t not in hq.STOPWORDS]
    hits = []
    for path, mtime in hq.session_paths():
        sid = os.path.splitext(os.path.basename(path))[0]
        for m in hq.iter_messages(path):
            low = m["text"].lower()
            score = sum(low.count(t) for t in terms)
            if score == 0 or not terms or terms[0] not in low:
                continue
            idx = min((low.find(t) for t in terms if t in low), default=0)
            hits.append({
                "score": score,
                "sid": sid,
                "project": hq.session_project(path),
                "date": mtime.astimezone().strftime("%m-%d"),
                "role": m["role"],
                "snippet": re.sub(r"\s+", " ", m["text"][max(0, idx - 80):idx + 220]).strip(),
            })
    hits.sort(key=lambda h: -h["score"])
    seen, out = set(), []
    for h in hits:
        k = (h["sid"], h["snippet"][:40])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= n:
            break
    return out


def api_session_text(sid):
    for path, _ in hq.session_paths():
        if os.path.basename(path).startswith(sid):
            blocks, total = [], 0
            for m in hq.iter_messages(path):
                blocks.append({"role": m["role"], "text": m["text"][:4000]})
                total += len(m["text"])
                if total > 150000:
                    break
            return blocks
    return []


def api_tree():
    def ls(root, kind):
        if not os.path.exists(root):
            return []
        if os.path.isfile(root):
            return [{"name": os.path.basename(root), "path": root.replace(HOME, "~"), "kind": kind}]
        return [
            {"name": os.path.relpath(f, root), "path": f.replace(HOME, "~"), "kind": kind}
            for f in sorted(glob.glob(os.path.join(root, "**", "*.md"), recursive=True))
        ]

    return {
        "agents": ls(os.path.join(GROK_HQ, "agents"), "agent"),
        "skills": ls(os.path.join(GROK_HQ, "skills"), "skill"),
        "digests": ls(os.path.join(GROK_HQ, "digests"), "digest")[:40],
        "policy": ls(os.path.join(GROK_HQ, "POLICY.md"), "policy"),
    }


def api_brief():
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        hq.cmd_brief(14)
    return {"brief": buf.getvalue()}


def api_catalog():
    cat = {"skills": [], "agents": [], "commands": [], "autopilot": os.path.exists(hq.AUTOPILOT_PID)}
    for f in sorted(glob.glob(os.path.join(GROK_HQ, "skills", "*", "SKILL.md"))):
        name = os.path.basename(os.path.dirname(f))
        cat["skills"].append({
            "name": name,
            "desc": "cross-session search via hq",
            "invoke": f"hq search \"…\"",
            "path": f.replace(HOME, "~"),
        })
    for f in sorted(glob.glob(os.path.join(GROK_HQ, "agents", "*.md"))):
        name = os.path.splitext(os.path.basename(f))[0]
        model = ""
        try:
            mm = re.search(r"^model:\s*(\S+)", open(f, encoding="utf-8").read(2000), re.M)
            model = mm.group(1) if mm else ""
        except OSError:
            pass
        cat["agents"].append({
            "name": name,
            "desc": "",
            "model": model,
            "invoke": f"delegate to {name}",
            "path": f.replace(HOME, "~"),
        })
    cat["commands"] = [
        {"name": "usage", "desc": "local usage estimate", "invoke": "hq usage"},
        {"name": "sessions", "desc": "list sessions", "invoke": "hq sessions"},
        {"name": "search", "desc": "search sessions", "invoke": "hq search \"query\""},
        {"name": "brief", "desc": "compiled briefing", "invoke": "hq brief"},
        {"name": "compile", "desc": "rebuild digests", "invoke": "hq compile"},
        {"name": "serve", "desc": "this panel", "invoke": "hq serve"},
        {"name": "autopilot", "desc": "background recompile", "invoke": "hq autopilot"},
    ]
    return cat


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p == "/":
                with open(os.path.join(HQ_DIR, "app.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif p == "/api/overview":
                self.send_json(api_overview(int(q.get("days", ["14"])[0])))
            elif p == "/api/sessions":
                self.send_json(api_sessions(int(q.get("n", ["30"])[0])))
            elif p == "/api/search":
                self.send_json(api_search(q.get("q", [""])[0]))
            elif p == "/api/session":
                self.send_json(api_session_text(q.get("id", [""])[0]))
            elif p == "/api/tree":
                self.send_json(api_tree())
            elif p == "/api/brief":
                self.send_json(api_brief())
            elif p == "/api/catalog":
                self.send_json(api_catalog())
            elif p == "/api/adapters":
                self.send_json({"text": hq.adapter_notes()})
            elif p == "/api/file":
                path = os.path.expanduser(unquote(q.get("path", [""])[0]))
                if not safe_path(path):
                    return self.send_json({"error": "path not allowed"}, 403)
                with open(path, encoding="utf-8") as f:
                    self.send_json({"content": f.read()})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/file":
                path = os.path.expanduser(data.get("path", ""))
                if not safe_path(path):
                    return self.send_json({"error": "path not allowed"}, 403)
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data.get("content", ""))
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    srv = HTTPServer(("127.0.0.1", port), Handler)
    print(f"Grok HQ control panel → http://127.0.0.1:{port}")
    print("  (stdlib http.server · no telemetry · Ctrl-C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
