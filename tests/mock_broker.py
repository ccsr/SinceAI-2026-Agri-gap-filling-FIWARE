#!/usr/bin/env python3
"""
mock_broker.py - minimal in-memory NGSI-LD broker for tests (not a real broker).

Covers what the scripts in this repository use: tenants (NGSILD-Tenant), batch upsert / update / delete,
GET /entities by type with limit, offset and count, GET /entities/{id}, GET /types.
It deliberately behaves like real brokers in the ways that have caused bugs:
  * entities come back in an arbitrary (hash) order
  * one-item arrays come back as plain values (JSON-LD compaction)
  * sub-attributes written as plain values are stored as Properties

  python3 tests/mock_broker.py [port]       # default 11026
"""
import collections
import http.server
import json
import sys
import urllib.parse

store = collections.defaultdict(dict)  # tenant -> id -> entity
type_iri = {}                           # short type -> expanded IRI (from the @context of upserts)
KEEP = ("type", "value", "object", "unitText", "unitCode")


def expand_type(e):
    ctx = e.get("@context")
    if isinstance(ctx, list):
        for c in ctx:
            if isinstance(c, dict) and e["type"] in c:
                iri = c[e["type"]]
                p, _, rest = iri.partition(":")
                type_iri[e["type"]] = c.get(p, p + ":") + rest if p in c else iri


def compact(o):
    if isinstance(o, dict):
        return {k: compact(v) for k, v in o.items()}
    if isinstance(o, list):
        return compact(o[0]) if len(o) == 1 else [compact(x) for x in o]
    return o


def normalise(e):
    out = {}
    for k, a in e.items():
        if isinstance(a, dict) and a.get("type") in ("Property", "Relationship", "GeoProperty"):
            a = {s: (v if s in KEEP or isinstance(v, dict) else {"type": "Property", "value": v}) for s, v in a.items()}
        out[k] = a
    return out


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, st, body=None, hdr=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(st)
        self.send_header("Content-Type", "application/json")
        for k, v in (hdr or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        t = self.headers.get("NGSILD-Tenant", "")
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/types"):
            return self.send(200, sorted({type_iri.get(e["type"], e["type"]) for e in store[t].values()}))
        if "/entities/" in u.path:
            e = store[t].get(urllib.parse.unquote(u.path.split("/entities/", 1)[1]))
            return self.send(200, compact(e)) if e else self.send(404, {"type": "ResourceNotFound"})
        typ = q.get("type", [None])[0]
        ents = [e for e in store[t].values() if typ in (None, e["type"], type_iri.get(e["type"]))]
        ents.sort(key=lambda e: hash(e["id"]))
        off, lim = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["20"])[0])
        page = [compact({k: v for k, v in e.items() if k != "@context"}) for e in ents[off:off + lim]]
        hdr = {"NGSILD-Results-Count": str(len(ents))} if q.get("count", ["false"])[0] == "true" else {}
        self.send(200, page, hdr)

    def do_POST(self):
        t = self.headers.get("NGSILD-Tenant", "")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
        if self.path.split("?")[0].endswith("/upsert"):
            for e in body:
                expand_type(e)
                store[t][e["id"]] = normalise(e)
            return self.send(204)
        if self.path.split("?")[0].endswith("/update"):
            missing = [e["id"] for e in body if e["id"] not in store[t]]
            for e in body:
                if e["id"] in store[t]:
                    store[t][e["id"]].update({k: v for k, v in normalise(e).items() if k not in ("id", "type", "@context")})
            if missing:
                return self.send(207, {"success": [], "errors": [{"entityId": i, "error": {"type": "ResourceNotFound"}}
                                                                 for i in missing]})
            return self.send(204)
        if self.path.split("?")[0].endswith("/delete"):
            for i in body:
                store[t].pop(i, None)
            return self.send(204)
        self.send(404, {"type": "ResourceNotFound"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11026
    print(f"mock NGSI-LD broker on http://127.0.0.1:{port}")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
