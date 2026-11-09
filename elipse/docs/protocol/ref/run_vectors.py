"""Corre las implementaciones de referencia contra ../vectors.json (Python en proceso, Node en un subproceso)."""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from verify_python import verify  # noqa: E402

V = json.loads((HERE.parent / "vectors.json").read_text(encoding="utf-8"))
casos = []
for p in V["positive"]:
    casos.append((p["name"], V["secret"], p["profile"], p["tool"], p["body"], p["ts"], p["signature"], p["ts"], True))
for n in V["negative"]:
    casos.append((n["name"], n["secret"], n["profile"], n["tool"], n["body"], n["ts"], n["signature"], n["now"], False))
for e in V["edge_valid"]:
    casos.append((e["name"], e["secret"], e["profile"], e["tool"], e["body"], e["ts"], e["signature"], e["now"], True))

fallos = 0
for name, secret, profile, tool, body, ts, sig, now, esperado in casos:
    got = verify(secret, profile, tool, body.encode("utf-8"), str(ts), sig, now=now)
    if got != esperado:
        fallos += 1
        print("PYTHON ✗", name, "esperado", esperado, "obtuvo", got)

js = r"""
const { verify } = require(process.argv[1]);
const casos = JSON.parse(require('fs').readFileSync(0, 'utf8'));
casos.forEach(c => console.log(verify(c.secret, c.profile, c.tool, Buffer.from(c.body, 'utf8'), String(c.ts), c.sig, c.now) ? '1' : '0'));
"""
entrada = json.dumps([dict(secret=c[1], profile=c[2], tool=c[3], body=c[4], ts=c[5], sig=c[6], now=c[7]) for c in casos])
r = subprocess.run(["node", "-e", js, str(HERE / "verify_node.js")], input=entrada, capture_output=True, text=True)
salida = r.stdout.split()
if len(salida) != len(casos):
    print("NODE ✗ salida inesperada:", r.stdout[:200], r.stderr[:300]); fallos += 1
else:
    for c, s in zip(casos, salida):
        if (s == "1") != c[8]:
            fallos += 1; print("NODE ✗", c[0])
print(f"{len(casos)} vectores × 2 implementaciones: " + ("TODO OK" if not fallos else f"{fallos} FALLOS"))
sys.exit(1 if fallos else 0)
