"""Dashboard server: snapshot, history and a Server-Sent Events feed.

Deliberately dependency-free (stdlib ``http.server``). Just enough to watch the
bot live or screen-record it, without adding a web framework to the hot path.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Optional

log = logging.getLogger("hlperp.server")

_INDEX = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>hl-perp-bot</title>
<style>
:root{--bg:#0b0d10;--fg:#e6e8eb;--dim:#7c838c;--buy:#28c76f;--sell:#ea5455;--warn:#ffb020;--acc:#97a0ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:16px}
h1{font-size:16px;margin:0 0 4px}p.sub{color:var(--dim);margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:16px}
.cell{border:1px solid #1e2230;border-radius:8px;padding:10px}
.k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.v{font-size:20px;font-variant-numeric:tabular-nums}
.buy{color:var(--buy)}.sell{color:var(--sell)}.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #1e2230;font-size:12px}
th{color:var(--dim);font-weight:400}
#banner{padding:8px;border-radius:8px;background:#3a2a00;color:var(--warn);margin-bottom:12px;display:none}
</style></head><body>
<h1>hl-perp-bot</h1>
<p class=sub id=sub>connecting…</p>
<div id=banner></div>
<div class=grid>
<div class=cell><div class=k>mid</div><div class=v id=mid>-</div></div>
<div class=cell><div class=k>spread bps</div><div class=v id=spread>-</div></div>
<div class=cell><div class=k>funding APR</div><div class=v id=funding>-</div></div>
<div class=cell><div class=k>position</div><div class=v id=pos>-</div></div>
<div class=cell><div class=k>equity</div><div class=v id=equity>-</div></div>
<div class=cell><div class=k>P&amp;L %</div><div class=v id=pnl>-</div></div>
<div class=cell><div class=k>fees</div><div class=v id=fees>-</div></div>
<div class=cell><div class=k>funding</div><div class=v id=fundingcost>-</div></div>
<div class=cell><div class=k>orders</div><div class=v id=orders>-</div></div>
<div class=cell><div class=k>fills</div><div class=v id=fills>-</div></div>
</div>
<div class=cell><div class=k>decision</div><div class=v id=decision style=font-size:14px>-</div></div>
<h3 style="font-size:13px;color:var(--dim)">tape</h3>
<table><thead><tr><th>time</th><th>coin</th><th>side</th><th>px</th><th>sz</th><th>maker</th><th>fee</th></tr></thead>
<tbody id=tape></tbody></table>
<script>
const es=new EventSource('/events');
es.onmessage=(e)=>{const m=JSON.parse(e.data);
 if(m.type==='snapshot'){document.getElementById('sub').textContent=m.meta.coin+' · '+m.meta.mode+' · '+m.meta.network;}
 if(m.type==='event'){render(m.data);}
 if(m.type==='fill'){addFill(m.data);}
};
function fmt(x,d=2){return x==null?'-':Number(x).toFixed(d);}
function render(e){
 mid.textContent=fmt(e.mid,1);spread.textContent=fmt(e.spread_bps,2);
 funding.textContent=fmt(e.funding_apr,1)+'%';
 const p=e.position||{};pos.textContent=(p.side||'flat')+' '+fmt(p.size,4);
 pos.className='v '+(p.side==='long'?'buy':p.side==='short'?'sell':'');
 const t=e.totals||{};equity.textContent=fmt(t.equity,2);pnl.textContent=fmt(t.pnl_pct,3)+'%';
 pnl.className='v '+((t.pnl_pct||0)>=0?'buy':'sell');
 fees.textContent=fmt(t.fees,4);fundingcost.textContent=fmt(t.funding,4);
 orders.textContent=t.orders;fills.textContent=t.fills;
 const d=e.decision;decision.textContent=d?`${d.action} (p_up ${fmt(d.probabilities.buy,2)}) ${fmt(d.latency_ms,0)}ms ${d.reason||''}`:'-';
 document.getElementById('banner').style.display=e.halted?'block':'none';
 document.getElementById('banner').textContent=e.halted?('HALTED: '+e.halt_reason):'';
}
function addFill(f){const tr=document.createElement('tr');
 tr.innerHTML=`<td>${new Date(f.ts).toLocaleTimeString()}</td><td>${f.coin}</td>
 <td class=${f.side==='buy'?'buy':'sell'}>${f.side}</td><td>${fmt(f.px,2)}</td>
 <td>${fmt(f.sz,4)}</td><td>${f.crossed?'taker':'maker'}</td><td>${fmt(f.fee,4)}</td>`;
 const tb=document.getElementById('tape');tb.prepend(tr);
 while(tb.rows.length>50)tb.deleteRow(tb.rows.length-1);}
</script></body></html>"""


class Server:
    def __init__(self, port: int, meta: dict, history_getter) -> None:
        self.port = port
        self.meta = meta
        self.history_getter = history_getter
        self.clients: list[Queue] = []
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None

    def broadcast(self, payload: dict) -> None:
        with self._lock:
            dead = []
            for q in self.clients:
                try:
                    q.put_nowait(payload)
                except Exception:
                    dead.append(q)
            for q in dead:
                self.clients.remove(q)

    def event(self, event) -> None:
        self.broadcast({"type": "event", "data": asdict(event)})

    def fill(self, fill) -> None:
        self.broadcast({"type": "fill", "data": asdict(fill)})

    def start(self, block: bool = False) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                if self.path == "/" or self.path == "/index.html":
                    body = _INDEX.encode()
                    self.send_response(200)
                    self.send_header("content-type", "text/html; charset=utf-8")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/snapshot":
                    snap = {"meta": server.meta, "history": [
                        asdict(e) for e in server.history_getter()[-100:]
                    ]}
                    body = json.dumps(snap).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/events":
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("cache-control", "no-cache")
                    self.end_headers()
                    q: Queue = Queue()
                    with server._lock:
                        server.clients.append(q)
                    try:
                        q.put({"type": "snapshot", "meta": server.meta})
                        for e in server.history_getter()[-100:]:
                            q.put({"type": "event", "data": asdict(e)})
                        while True:
                            item = q.get()
                            self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                            self.wfile.flush()
                    except Exception:
                        pass
                    finally:
                        with server._lock:
                            if q in server.clients:
                                server.clients.remove(q)
                else:
                    self.send_error(404)

        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
        log.info("dashboard on http://localhost:%d", self.port)
        if block:
            t.join()
