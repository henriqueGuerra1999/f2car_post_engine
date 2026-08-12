"""
Microservico Fenrion -- gerador de posts Instagram (F2Car)
------------------------------------------------------------
Empacota generate_post.py + template_config.json + logo como um endpoint
HTTP simples, para o N8N poder chamar via HTTP Request node e receber o
PNG pronto a publicar. E o mesmo motor que ja foi validado localmente --
esta camada so acrescenta a interface HTTP.

Endpoints:
  GET  /health                -> {"status": "ok"}  (para o N8N/monitorizacao confirmar que esta vivo)
  POST /gerar-post            -> recebe JSON da viatura, devolve PNG (image/png)

Formato esperado no corpo do POST (igual ao dict `vehicle` que generate_post.py
ja usa, mais um `photo_url` opcional para o servico ir buscar a foto real):
{
  "model": "BMW M2",
  "fuel": "Gasolina", "power": "480cv", "km": "7.000kms", "year": "2025", "gearbox": "Auto",
  "condition": "NACIONAL",
  "price": "89.900€",
  "old_price": "99.000€" | null,
  "photo_url": "https://spaces.onepilot.app/.../xlarge/....webp" | null
}

Correr localmente para testar:
    pip install fastapi uvicorn pillow requests --break-system-packages
    uvicorn service:app --host 0.0.0.0 --port 8000

Deploy (Render/Railway, ou qualquer host com Docker/Python):
  - Usar o Dockerfile ao lado, ou o comando de arranque:
    uvicorn service:app --host 0.0.0.0 --port $PORT
"""
import io
import os
import tempfile
import uuid

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from generate_post import render_post

app = FastAPI(title="Fenrion F2Car Post Generator")


class VehiclePayload(BaseModel):
    model: str
    fuel: str = "-"
    power: str = "-"
    km: str = "-"
    year: str = "-"
    gearbox: str = "-"
    condition: str = "NACIONAL"
    price: str
    old_price: str | None = None
    photo_url: str | None = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/gerar-post")
def gerar_post(payload: VehiclePayload):
    vehicle = payload.model_dump()
    photo_url = vehicle.pop("photo_url", None)

    tmp_photo_path = None
    if photo_url:
        try:
            resp = requests.get(photo_url, timeout=15)
            resp.raise_for_status()
            tmp_photo_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.jpg")
            # PIL abre webp/jpg/png automaticamente a partir dos bytes
            from PIL import Image
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img.save(tmp_photo_path, "JPEG", quality=92)
        except Exception as e:
            # Nao falha o post todo por causa da foto -- usa o placeholder
            # honesto que o motor ja tem, e regista o motivo no proprio erro
            # devolvido no header para facilitar debug no N8N.
            tmp_photo_path = None
            vehicle["_photo_error"] = str(e)

    vehicle["photo_path"] = tmp_photo_path

    out_path = os.path.join(tempfile.gettempdir(), f"post_{uuid.uuid4()}.png")
    try:
        render_post(vehicle, out_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha a gerar o post: {e}")
    finally:
        if tmp_photo_path and os.path.exists(tmp_photo_path):
            os.remove(tmp_photo_path)

    with open(out_path, "rb") as f:
        png_bytes = f.read()
    os.remove(out_path)

    headers = {}
    if vehicle.get("_photo_error"):
        headers["X-Photo-Warning"] = vehicle["_photo_error"][:200]

    return Response(content=png_bytes, media_type="image/png", headers=headers)
