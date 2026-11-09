"""Protocolo de callback (docs/protocol): los vectores deben coincidir con el motor real.
Si alguien cambia el formato del cuerpo, los headers o la firma en callback.py, este test falla y obliga a
actualizar PROTOCOL.md y vectors.json (y a subir la versión del protocolo si el cambio no es compatible)."""
import json
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.core import callback, db, profiles

VECTORS = json.loads((Path(__file__).resolve().parent.parent / "docs" / "protocol" / "vectors.json")
                     .read_text(encoding="utf-8"))
SECRET = VECTORS["secret"]


def test_firma_de_los_vectores_positivos():
    for p in VECTORS["positive"]:
        body = p["body"].encode("utf-8")
        assert body.hex() == p["body_utf8_hex"]
        assert callback.sign(SECRET, p["ts"], p["profile"], p["tool"], body) == p["signature"], p["name"]
        assert callback.verify_request(SECRET, p["profile"], p["tool"], body, str(p["ts"]), p["signature"], now=p["ts"]), p["name"]


def test_vectores_negativos_se_rechazan():
    for n in VECTORS["negative"]:
        assert not callback.verify_request(n["secret"], n["profile"], n["tool"], n["body"].encode("utf-8"),
                                           str(n["ts"]), n["signature"], now=n["now"]), n["name"]


def test_vectores_de_borde_se_aceptan():
    for e in VECTORS["edge_valid"]:
        assert callback.verify_request(e["secret"], e["profile"], e["tool"], e["body"].encode("utf-8"),
                                       str(e["ts"]), e["signature"], now=e["now"]) is e["valid"], e["name"]


# ----------------------------------------------------------------------------- lo que sale por el cable
class _Captura(BaseHTTPRequestHandler):
    visto = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).visto = {"path": self.path, "body": body, "headers": dict(self.headers.items())}
        data = b'{"ok":true,"result":"ok"}'
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(data)


class _FakeUUID:
    def __init__(self, h):
        self.hex = h


@pytest.fixture
def anfitrion(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    from app.core.seed_personality import seed
    seed()
    srv = HTTPServer(("127.0.0.1", 0), _Captura)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/elipse/tools"
    srv.shutdown()


def test_el_motor_emite_exactamente_los_bytes_y_headers_de_los_vectores(anfitrion, monkeypatch):
    nombres = sorted({p["tool"] for p in VECTORS["positive"]})
    spec = {"role": "x", "tools": {"host": [{"name": t, "description": "d", "risk": "read",
                                             "parameters": {"type": "object", "properties": {}}} for t in nombres]}}
    perfil = VECTORS["positive"][0]["profile"]
    profiles.sync_profile(perfil, spec, None)
    profiles.set_callback(perfil, anfitrion, SECRET)
    prof = profiles.profile_from_key(perfil, {"host_tools": True})

    for p in VECTORS["positive"]:
        wi = p["wire_inputs"]
        monkeypatch.setattr(callback, "time", types.SimpleNamespace(time=lambda ts=p["ts"]: ts, monotonic=time.monotonic))
        monkeypatch.setattr(callback, "uuid", types.SimpleNamespace(uuid4=lambda cid=wi["call_id"]: _FakeUUID(cid)))
        assert callback.call_host_tool(prof, p["tool"], wi["args"], wi["actor"], wi["session"]) == "ok"
        visto = _Captura.visto
        assert visto["path"] == p["path"], p["name"]
        assert visto["body"] == bytes.fromhex(p["body_utf8_hex"]), p["name"]
        assert visto["headers"]["X-Elipse-Ts"] == str(p["ts"])
        assert visto["headers"]["X-Elipse-Sig"] == p["signature"], p["name"]
        assert visto["headers"]["Content-Type"] == "application/json; charset=utf-8"
