# Copyright (c) 2026 BANFi Semiconductor Co., Ltd.
# Copyright (c) 2026 FL SEMICONDUCTOR LLC (FLSEMI)
# SPDX-License-Identifier: BSD-3-Clause
"""gwstage.py - a local demo wall for a BFi91XTD mesh.

One process owns the gateway's USB port, polls the sink's node table, and
serves the result to as many browsers as want it. The gateway's own cloud
uplink is unaffected: it runs on the gateway's Wi-Fi, not on this cable,
so the mesh can be on ThingsBoard and on this wall at the same time.

Read-only by construction -- there is no write path in this file. A demo
that can reconfigure the thing being demonstrated is a demo that ends
badly, and `xtd cfg` is one terminal away when a change is wanted.
"""
import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import gwcfg as G

# The sense flags say which readings on a row are real. A row without the
# bit is a node that has no such sensor (or has not sent one yet), not a
# node reading zero -- drawing 0 °C for it would be inventing data.
F_ENV, F_VBAT, F_IAQ, F_DIE = 0x01, 0x02, 0x04, 0x08

# The gateway reports its own sample under this id (it has no RD id of its
# own on the air -- it is the sink's host). Replaced by the sink's real
# Long RD id once we have read it.
SELF_RDID = 0xFFFFFFFF

HISTORY = 720          # samples kept per node
OFFLINE_S = 120        # a node unheard for this long is drawn as lost

STATE = {
    "sink": None,        # {rdid, carrier, band, network, hop}
    "nodes": {},         # rdid -> latest row (+ label, depth, online)
    "history": {},       # rdid -> [[t, temp, hum, iaq, co2, vbat], ...]
    "polled": 0.0,
    "error": None,
    "dev": "",
    "site": "",
}
LOCK = threading.Lock()


def _num(row, key, scale=1.0, flag=None):
    """A reading, or None when the flags say the node did not send one."""
    if flag is not None and not (row.get("flags", 0) & flag):
        return None
    if key not in row:
        return None
    return row[key] / scale


def poll_once(dev, names):
    sense = G.command(dev, G.OP_READ, G.ID_SENSE, {}, timeout=6.0)
    dect = G.command(dev, G.OP_READ, G.ID_DECT, {}, timeout=6.0)
    chip = G.command(dev, G.OP_READ, G.ID_CHIPID, {}, timeout=6.0)
    sink_rdid = chip.get("modem_id", 0) or 0

    now = time.time()
    rows = {}
    for r in sense.get("nodes", []):
        rdid = r.get("rdid", 0)
        if rdid == SELF_RDID:
            rdid = sink_rdid          # the gateway's own row IS the sink
            r = dict(r, parent=0)
        rows[rdid] = r

    nodes, hist = {}, STATE["history"]
    for rdid, r in rows.items():
        key = "%08x" % rdid
        n = {
            "rdid": key,
            "label": names.get(key, names.get(key.upper(), "")),
            "parent": "%08x" % r.get("parent", 0) if r.get("parent") else None,
            "up": r.get("up", 0),
            "age": r.get("age", 0),
            "seq": r.get("seq", 0),
            "online": r.get("age", 9999) < OFFLINE_S,
            "is_sink": rdid == sink_rdid,
            "temp": _num(r, "temp", 100.0, F_ENV),
            "hum": _num(r, "hum", 100.0, F_ENV),
            "press": _num(r, "press", 100.0, F_ENV),
            "iaq": _num(r, "iaq", 1.0, F_IAQ),
            "co2": _num(r, "co2", 1.0, F_IAQ),
            "voc": _num(r, "voc", 1.0, F_IAQ),
            "vbat": _num(r, "vbat", 1000.0, F_VBAT),
            "die": _num(r, "die", 1.0, F_DIE),
            "supply": (r.get("supply") or 0) / 1000.0 or None,
        }
        nodes[key] = n
        if n["online"]:
            h = hist.setdefault(key, [])
            last = h[-1] if h else None
            sample = [round(now), n["temp"], n["hum"], n["iaq"], n["co2"], n["vbat"]]
            # one point per reported sample, not one per poll: the node's
            # own seq is what moved, and a flat line of duplicates would
            # make a dead sensor look alive
            if last is None or r.get("seq", 0) != last[-1]:
                h.append(sample + [r.get("seq", 0)])
                del h[:-HISTORY]

    # depth from each node's own answer about its parent, not from the plan
    for n in nodes.values():
        d, seen, cur = 0, set(), n
        while cur and cur["parent"] and cur["parent"] in nodes and cur["rdid"] not in seen:
            seen.add(cur["rdid"])
            cur = nodes[cur["parent"]]
            d += 1
        n["depth"] = d

    with LOCK:
        STATE["sink"] = {
            "rdid": "%08x" % sink_rdid,
            "carrier": dect.get("carrier"),
            "band": dect.get("band"),
            "network": "0x%08x" % dect.get("network", 0),
            "hop": dect.get("hop_stored"),
        }
        STATE["nodes"] = nodes
        STATE["polled"] = now
        STATE["error"] = None


def poller(dev, names, interval):
    while True:
        try:
            poll_once(dev, names)
        except Exception as e:
            with LOCK:
                STATE["error"] = str(e)
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            with LOCK:
                self._send(200, json.dumps({
                    "sink": STATE["sink"], "nodes": list(STATE["nodes"].values()),
                    "polled": STATE["polled"], "error": STATE["error"],
                    "dev": STATE["dev"], "site": STATE["site"],
                    "now": time.time()}))
            return
        m = re.match(r"^/api/history/([0-9a-f]{8})$", path)
        if m:
            with LOCK:
                self._send(200, json.dumps(STATE["history"].get(m.group(1), [])))
            return
        self._send(404, json.dumps({"error": "no such path"}))


PAGE = r"""<!doctype html><meta charset="utf-8">
<title>BFi91XTD mesh</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#070b14;--panel:#0d1424;--line:#1b2740;--fg:#dce6f5;--dim:#7a8aa5;
      --ok:#3ddc97;--warn:#ffc857;--bad:#ff6b6b;--sink:#5aa9ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.5 ui-sans-serif,-apple-system,"Helvetica Neue",sans-serif}
header{display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;
       padding:16px 22px;border-bottom:1px solid var(--line)}
h1{font-size:19px;margin:0;letter-spacing:.14em;text-transform:uppercase}
.meta{color:var(--dim);font-size:13px}
.meta b{color:var(--fg);font-weight:600}
main{display:grid;grid-template-columns:minmax(340px,38%) 1fr;gap:18px;padding:18px 22px}
@media(max-width:1000px){main{grid-template-columns:1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
h2{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);
   margin:0 0 14px}
svg{width:100%;height:auto;display:block}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card{background:#0a1120;border:1px solid var(--line);border-radius:10px;padding:13px 14px}
.card.off{opacity:.42}
.card h3{margin:0 0 2px;font-size:15px;display:flex;justify-content:space-between;
         align-items:baseline;gap:8px}
.card .id{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim)}
.row{display:flex;justify-content:space-between;font-size:13px;padding:2px 0}
.row span:first-child{color:var(--dim)}
.big{font-size:27px;font-weight:600;margin:6px 0 2px}
.big small{font-size:14px;color:var(--dim);font-weight:400}
.pill{font-size:10px;letter-spacing:.1em;text-transform:uppercase;padding:2px 7px;
      border-radius:99px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.sink{color:var(--sink)}
footer{padding:10px 22px 26px;color:var(--dim);font-size:12px}
</style>
<header>
  <h1>BFi91XTD mesh</h1>
  <div class="meta" id="hdr">connecting…</div>
</header>
<main>
  <section><h2>Topology</h2><svg id="topo" viewBox="0 0 400 300"></svg></section>
  <section><h2>Nodes</h2><div class="cards" id="cards"></div></section>
</main>
<footer id="foot"></footer>
<script>
const $=s=>document.querySelector(s);
let HIST={};

// Bosch IAQ bands, the same ones the kit's LED uses.
function iaqClass(v){ return v==null?"":v<=100?"ok":v<=200?"warn":"bad"; }
function ageClass(n){ return !n.online?"bad":n.age>30?"warn":"ok"; }

function hhmm(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`;}

function layout(nodes){
  // Each node's own answer about its parent builds the tree; a node whose
  // parent we cannot see hangs off the root so it is visible rather than
  // silently dropped.
  const by={},kids={};
  nodes.forEach(n=>{by[n.rdid]=n;kids[n.rdid]=[]});
  let root=nodes.find(n=>n.is_sink);
  nodes.forEach(n=>{ if(n===root) return;
    const p=(n.parent&&by[n.parent])?n.parent:(root?root.rdid:null);
    if(p) kids[p].push(n); });
  Object.values(kids).forEach(a=>a.sort((x,y)=>x.rdid.localeCompare(y.rdid)));
  return {by,kids,root};
}

function drawTopo(nodes){
  const {kids,root}=layout(nodes);
  if(!root){ $("#topo").innerHTML=""; return; }
  // A tidy tree: leaves take the next column, a parent sits centred over
  // its children. A chain of relays then draws as a straight line down,
  // which is what the mesh actually is.
  const W=400, rowH=62, pad=26;
  const col={}, depth={}; let next=0, maxD=0;
  (function place(n,d){
    depth[n.rdid]=d; maxD=Math.max(maxD,d);
    const ch=kids[n.rdid]||[];
    if(!ch.length){ col[n.rdid]=next++; return; }
    ch.forEach(k=>place(k,d+1));
    col[n.rdid]=(col[ch[0].rdid]+col[ch[ch.length-1].rdid])/2;
  })(root,0);
  const cols=Math.max(1,next);
  const H=Math.max(140,(maxD+1)*rowH+pad);
  const pos={};
  nodes.forEach(n=>{ if(col[n.rdid]===undefined) return;
    pos[n.rdid]=[pad+(W-2*pad)*(cols===1?.5:(col[n.rdid]+.5)/cols), 26+depth[n.rdid]*rowH]; });
  let g="";
  nodes.forEach(n=>{
    const p=pos[n.rdid]; if(!p) return;
    (kids[n.rdid]||[]).forEach(k=>{
      const q=pos[k.rdid]; if(!q) return;
      g+=`<path d="M${p[0]} ${p[1]+13} C${p[0]} ${p[1]+38}, ${q[0]} ${q[1]-38}, ${q[0]} ${q[1]-13}"
           fill="none" stroke="${k.online?'#24406b':'#3a2330'}" stroke-width="1.6"/>`;
    });
  });
  nodes.forEach(n=>{
    const p=pos[n.rdid]; if(!p) return;
    const col=n.is_sink?"var(--sink)":n.online?"var(--ok)":"var(--bad)";
    g+=`<g><circle cx="${p[0]}" cy="${p[1]}" r="${n.is_sink?11:8}" fill="${col}"
          fill-opacity="${n.online?.22:.12}" stroke="${col}" stroke-width="1.8"/>`;
    if(n.online&&n.age<=30)
      g+=`<circle cx="${p[0]}" cy="${p[1]}" r="8" fill="none" stroke="${col}" stroke-width="1.2">
          <animate attributeName="r" values="8;17" dur="2.4s" repeatCount="indefinite"/>
          <animate attributeName="opacity" values=".55;0" dur="2.4s" repeatCount="indefinite"/></circle>`;
    g+=`<text x="${p[0]}" y="${p[1]+25}" text-anchor="middle" font-size="9.5"
          fill="var(--fg)">${n.label||n.rdid}</text>`;
    if(n.label) g+=`<text x="${p[0]}" y="${p[1]+35}" text-anchor="middle" font-size="8"
          fill="var(--dim)" font-family="ui-monospace,Menlo,monospace">${n.rdid}</text>`;
    g+="</g>";
  });
  const s=$("#topo"); s.setAttribute("viewBox",`0 0 ${W} ${H}`); s.innerHTML=g;
}

function spark(h,idx,col){
  const pts=h.map(r=>r[idx]).filter(v=>v!=null);
  if(pts.length<2) return "";
  const lo=Math.min(...pts),hi=Math.max(...pts),span=(hi-lo)||1;
  const d=pts.map((v,i)=>`${i/(pts.length-1)*100},${26-(v-lo)/span*22}`).join(" ");
  return `<svg viewBox="0 0 100 28" preserveAspectRatio="none" style="height:28px;margin-top:6px">
    <polyline points="${d}" fill="none" stroke="${col}" stroke-width="1.6"
      vector-effect="non-scaling-stroke"/></svg>`;
}

let kidsOf={};

function card(n){
  const h=HIST[n.rdid]||[];
  const env=n.temp!=null, air=n.iaq!=null;
  let body="";
  if(env) body+=`<div class="big">${n.temp.toFixed(1)}<small> °C</small>
     <span style="font-size:15px;color:var(--dim)">&nbsp;${n.hum.toFixed(0)}% RH</span></div>`
     +spark(h,1,"var(--sink)");
  if(air) body+=`<div class="row"><span>air quality</span>
      <span class="${iaqClass(n.iaq)}">IAQ ${n.iaq}</span></div>
    <div class="row"><span>CO₂ / VOC</span><span>${n.co2} ppm / ${n.voc} ppb</span></div>`;
  if(n.press!=null) body+=`<div class="row"><span>pressure</span><span>${n.press.toFixed(1)} hPa</span></div>`;
  if(!env&&!air) body+=`<div class="big" style="font-size:19px;color:var(--dim)">`
    +`${(kidsOf[n.rdid]||0)?"forwarding for "+kidsOf[n.rdid]:"no sensor reported"}</div>`;
  if(n.die!=null) body+=`<div class="row"><span>modem die</span><span>${n.die} °C</span></div>`;
  if(n.vbat!=null) body+=`<div class="row"><span>battery</span><span>${n.vbat.toFixed(2)} V</span></div>`;
  else if(n.supply!=null) body+=`<div class="row"><span>supply</span><span>${n.supply.toFixed(2)} V</span></div>`;
  return `<div class="card ${n.online?"":"off"}">
    <h3>${n.label||n.rdid}
      <span class="pill ${n.is_sink?"sink":ageClass(n)}">${n.is_sink?"sink":n.online?"hop "+n.depth:"lost"}</span></h3>
    <div class="id">${n.rdid}${n.parent?" ← "+n.parent:""}</div>
    ${body}
    <div class="row" style="margin-top:8px;border-top:1px solid var(--line);padding-top:6px">
      <span>up ${hhmm(n.up)}</span><span class="${ageClass(n)}">heard ${n.age}s ago</span></div>
  </div>`;
}

async function tick(){
  let s;
  try{ s=await (await fetch("/api/state")).json(); }
  catch(e){ $("#hdr").innerHTML=`<span class="bad">no answer from the local server</span>`; return; }
  const nodes=s.nodes.sort((a,b)=>(b.is_sink-a.is_sink)||a.depth-b.depth||a.rdid.localeCompare(b.rdid));
  await Promise.all(nodes.filter(n=>n.temp!=null||n.iaq!=null).map(async n=>{
    HIST[n.rdid]=await (await fetch("/api/history/"+n.rdid)).json();
  }));
  const on=nodes.filter(n=>n.online).length;
  const k=s.sink||{};
  $("#hdr").innerHTML=`<b>${on}</b>/${nodes.length} nodes &nbsp;·&nbsp; `
    +`network <b>${k.network||"?"}</b> &nbsp;·&nbsp; carrier <b>${k.carrier||"?"}</b> (band ${k.band||"?"})`
    +(s.site?` &nbsp;·&nbsp; ${s.site}`:"")
    +(s.error?` &nbsp;·&nbsp; <span class="bad">${s.error}</span>`:"");
  kidsOf={}; nodes.forEach(n=>{ if(n.parent) kidsOf[n.parent]=(kidsOf[n.parent]||0)+1; });
  drawTopo(nodes);
  $("#cards").innerHTML=nodes.map(card).join("");
  const agoS=Math.max(0,Math.round(s.now-s.polled));
  $("#foot").textContent=`read over ${s.dev} ${agoS}s ago — every value on this page came off `
    +`the wire from the mesh in front of you; the gateway's own cloud uplink is untouched by it`;
}
tick(); setInterval(tick,3000);
</script>
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--dev", default=None,
                    help="the sink gateway's SMP port (this process owns it)")
    ap.add_argument("-p", "--port", type=int, default=8770)
    ap.add_argument("-i", "--interval", type=float, default=3.0,
                    help="seconds between polls of the node table (default 3)")
    ap.add_argument("--names", metavar="FILE", default=None,
                    help='JSON: {"06aa8c8f": "Lobby", "5d2a5579": "Stairwell"} '
                         "-- rd ids the demo should show by name")
    ap.add_argument("--site", default="", help="site name for the header")
    args = ap.parse_args()

    dev = args.dev or G.default_dev()
    names = {}
    if args.names:
        names = {k.lower(): v for k, v in json.load(open(args.names)).items()}

    STATE["dev"] = dev
    STATE["site"] = args.site
    try:
        poll_once(dev, names)      # fail loudly here, not in a browser tab
    except Exception as e:
        sys.exit("xtd stage: %s" % e)

    threading.Thread(target=poller, args=(dev, names, args.interval),
                     daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("stage on http://127.0.0.1:%d  (%d nodes over %s, read-only)"
          % (args.port, len(STATE["nodes"]), dev))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0
