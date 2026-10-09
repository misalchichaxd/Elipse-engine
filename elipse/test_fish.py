from pathlib import Path
import httpx

p = Path(__file__).parent / ".env"
print("Leyendo:", p, "| existe:", p.exists())
env = dict(l.strip().split("=", 1) for l in p.read_text(encoding="utf-8").splitlines()
           if "=" in l and not l.strip().startswith("#"))
r = httpx.post("https://api.fish.audio/v1/tts",
    headers={"Authorization": "Bearer " + env["VOICE_FISH_API_KEY"],
             "model": env.get("VOICE_FISH_MODEL", "s1")},
    json={"text": "Hola, prueba de voz", "format": "mp3",
          "reference_id": env.get("VOICE_FISH_VOICE_ID", "")},
    timeout=30)
print(r.status_code)
print("OK", len(r.content), "bytes" if r.status_code == 200 else r.text[:300])