import base64, sys
from ollama import Client

img = base64.b64encode(open(sys.argv[1], "rb").read()).decode()
r = Client(timeout=300).chat(
    model="qwen3-vl:2b",
    messages=[{"role": "user", "content": "Describe esta imagen en español.", "images": [img]}],
    think=False,
    options={"num_ctx": 4096, "num_predict": 450},
)
m = r["message"]
print("CONTENT :", repr(m.get("content")))
print("THINKING:", repr(m.get("thinking"))[:500])
print("done_reason:", r.get("done_reason"))