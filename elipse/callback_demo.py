"""
Anfitrión de ejemplo para probar el callback SIN Django (usa solo la librería estándar).
Verifica la firma exactamente como lo hará el SDK y responde a tres tools de juguete.

Uso:
  1) python callback_demo.py <SECRETO>            (escucha en http://127.0.0.1:8765/elipse/tools)
  2) python -m app.manage_profiles set-callback rbe --url http://127.0.0.1:8765/elipse/tools --secret <SECRETO>
  3) sincroniza el perfil 'rbe' con una tool de anfitrión (PUT /v1/profiles/rbe) y chatea.
El secreto debe tener al menos 16 caracteres.
"""
import hashlib
import hmac
import json
import sys
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

PROFILE = "rbe"
SECRET = sys.argv[1] if len(sys.argv) > 1 else sys.exit("Uso: python callback_demo.py <SECRETO>")


def verify(tool: str, body: bytes, ts, sig) -> bool:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > 60:
        return False
    msg = f"{ts}.{PROFILE}.{tool}.{hashlib.sha256(body).hexdigest()}"
    good = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, str(sig or ""))


TOOLS = {
    "viajes_del_dia": lambda a, ctx: [{"viaje": 101, "fecha": a.get("fecha") or str(date.today()), "pasajeros": 12}],
    "quien_pregunta": lambda a, ctx: f"actor={ctx['actor']!r} session={ctx['session']!r}",
    "fallar": lambda a, ctx: (_ for _ in ()).throw(ValueError("fallo de prueba")),
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        tool = self.path.rsplit("/", 1)[-1]
        if not verify(tool, body, self.headers.get("X-Elipse-Ts"), self.headers.get("X-Elipse-Sig")):
            return self._send(401, {"ok": False, "error": "firma inválida"})
        req = json.loads(body)
        print(f"[callback] {tool} args={req['args']} actor={req['actor']!r} session={req['session']!r}")
        fn = TOOLS.get(tool)
        if not fn:
            return self._send(404, {"ok": False, "error": f"tool desconocida: {tool}"})
        try:
            self._send(200, {"ok": True, "result": fn(req["args"], req)})
        except Exception as e:
            self._send(200, {"ok": False, "error": str(e)})

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("Escuchando en http://127.0.0.1:8765/elipse/tools  (Ctrl+C para salir)")
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
