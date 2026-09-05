# ruff: noqa: E501  -- the page is one inline HTML string
"""A read-only live view of the draft, served on the LAN.

    ./.venv/bin/python scripts/watch_web.py          # http://<box>:8787

Reads the loop's own files (clock.json, events.jsonl, verdicts/, the log) and
nothing else. It cannot write, and it never touches ESPN.
"""
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8787


def newest_dir():
    ds = sorted(DATA.glob("drafts/*-live/"), key=os.path.getmtime)
    return ds[-1] if ds else None


def loop_up():
    return os.system("pgrep -f scripts/draft.py >/dev/null") == 0


def state():
    d = newest_dir()
    out = {"now": time.strftime("%H:%M:%S"),
           "enabled": (ROOT / "ENABLED").read_text().strip(),
           "loop_up": loop_up(), "clock": None, "queue": None,
           "picks": [], "verdicts": [], "log": []}
    if not d:
        return out
    try:
        out["clock"] = json.loads((d / "clock.json").read_text())
    except Exception:
        pass
    try:
        evs = [json.loads(line) for line in (d / "events.jsonl").read_text().splitlines()
               if line.strip()]
        qs = [e for e in evs if e.get("event") == "queue_sync"]
        if qs:
            out["queue"] = qs[-1]
    except Exception:
        pass
    try:
        vs = []
        for f in sorted((d / "verdicts").glob("*.json")):
            v = json.loads(f.read_text())
            v["_file"] = f.name
            vs.append(v)
        out["verdicts"] = vs[-6:]
    except Exception:
        pass
    try:
        lines = (DATA / "draft-live.log").read_text(errors="replace").splitlines()
        out["picks"] = [re.sub(r".*round", "round", ln) for ln in lines
                        if "EXECUTED draft_pick" in ln]
        out["log"] = [ln[:220] for ln in lines[-25:]]
    except Exception:
        pass
    return out


PAGE = """<!doctype html><meta charset=utf-8><title>big P live draft</title>
<style>
body{margin:0;background:#0d1117;color:#e6edf3;font:15px/1.4 ui-monospace,Menlo,Consolas,monospace}
header{display:flex;gap:24px;align-items:center;padding:12px 20px;background:#161b22;border-bottom:1px solid #30363d;position:sticky;top:0}
.pill{padding:2px 10px;border-radius:12px;font-weight:700}
.up{background:#1f6f3f}.down{background:#8b1e1e}.on{background:#8a6d00}.off{background:#444}
.live{background:#c2410c;animation:p 1.2s infinite}.wait{background:#30363d}.done{background:#1f6f3f}
@keyframes p{50%{opacity:.55}}
main{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:18px 20px}
section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
h2{margin:0 0 10px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#8b949e}
ol{margin:0;padding-left:26px}li{padding:2px 0}
.big{font-size:28px;font-weight:700}.turn{color:#f0b429;font-weight:700}
pre{margin:0;white-space:pre-wrap;color:#8b949e;font-size:12px}
.v{border-left:3px solid #388bfd;padding:4px 10px;margin:6px 0;background:#0d1117}
.wide{grid-column:1/3}.dim{color:#8b949e;font-size:12px;margin-top:8px}
</style>
<header><span class=big>big P</span><span id=draft class=pill>...</span><span id=loop class=pill>...</span><span id=en class=pill>...</span><span id=clock></span><span id=now style="margin-left:auto;color:#8b949e"></span></header>
<main>
<section><h2>Our ESPN queue (autopick draws from the top)</h2><ol id=queue></ol><div id=qmeta class=dim></div></section>
<section><h2>Our picks</h2><ol id=picks></ol></section>
<section class=wide><h2>Judge</h2><div id=verdicts></div></section>
<section class=wide><h2>Log</h2><pre id=log></pre></section>
</main>
<script>
async function tick(){
 try{
  const s=await (await fetch("/state.json",{cache:"no-store"})).json();
  const L=document.getElementById("loop");L.textContent=s.loop_up?"LOOP UP":"LOOP DOWN";L.className="pill "+(s.loop_up?"up":"down");
  const E=document.getElementById("en");E.textContent="ENABLED "+s.enabled;E.className="pill "+(s.enabled==="on"?"on":"off");
  const c=s.clock||{};
  const D=document.getElementById("draft");const started=(c.next_overall||1)>1||s.picks.length>0;
  D.textContent=c.complete?"DRAFT COMPLETE":started?"DRAFT LIVE":"WAITING FOR DRAFT";D.className="pill "+(c.complete?"done":started?"live":"wait");
  document.getElementById("clock").innerHTML=c.our_turn?'<span class=turn>ON THE CLOCK</span>':
    "round "+(c.round_num??"?")+" &middot; next overall #"+(c.next_overall??"?")+" &middot; "+(c.picks_until_our_turn??"?")+" picks until us &middot; pace "+(c.pace_s??"?")+"s";
  document.getElementById("now").textContent="box "+s.now;
  const q=s.queue||{target:[]};
  document.getElementById("queue").innerHTML=q.target.map(n=>"<li>"+n+"</li>").join("");
  document.getElementById("qmeta").textContent=q.at?("landed "+q.landed+"/"+q.planned+" at "+q.at.slice(11,19)+"Z"):"no sync yet";
  document.getElementById("picks").innerHTML=s.picks.map(p=>"<li>"+p+"</li>").join("")||"<li class=dim style='list-style:none'>none yet</li>";
  document.getElementById("verdicts").innerHTML=s.verdicts.map(v=>"<div class=v><b>"+v._file+"</b> &middot; "+(v.decision??v.verdict??"")+"<br>"+String(v.reasoning||v.summary||JSON.stringify(v)).slice(0,600)+"</div>").join("")||"<span class=dim>no verdicts yet</span>";
  document.getElementById("log").textContent=s.log.join("\\n");
 }catch(e){document.getElementById("loop").textContent="NO DATA";}
}
tick();setInterval(tick,3000);
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/state.json"):
            body, ct = json.dumps(state()).encode(), "application/json"
        else:
            body, ct = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"serving on 0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
