"""
Microservico Fenrion -- gerador de posts Instagram (F2Car)
------------------------------------------------------------
Empacota generate_post.py + template_config.json + logo como um endpoint
HTTP simples, para o N8N poder chamar via HTTP Request node e receber o
PNG pronto a publicar. E o mesmo motor que ja foi validado localmente --
esta camada so acrescenta a interface HTTP.

Endpoints:
  GET  /health                -> {"status": "ok"}  (para o N8N/monitorizacao confirmar que esta vivo)
  POST /gerar-post            -> recebe JSON da viatura, devolve PNG (image/png) diretamente
  POST /gerar-post-url        -> igual, mas devolve {"image_url": "https://.../imagens/<id>.png"}
                                  -- usar este quando o destino for a Instagram Graph API, que so
                                  aceita publicar a partir de uma imagem ja hospedada publicamente
                                  (nao aceita upload binario direto no fluxo simples de publicacao).
  GET  /imagens/{ficheiro}    -> serve as imagens geradas por /gerar-post-url

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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generate_post import render_post

app = FastAPI(title="Fenrion F2Car Post Generator")

# Pasta onde ficam as imagens geradas por /gerar-post-url, servidas
# estaticamente em /imagens/<ficheiro>.png. Vive no disco efemero da
# instancia -- suficiente porque a Instagram Graph API vai buscar a
# imagem segundos depois de ser criada (nao precisa de durar para sempre).
_IMAGES_DIR = os.path.join(tempfile.gettempdir(), "fenrion_imagens")
os.makedirs(_IMAGES_DIR, exist_ok=True)
app.mount("/imagens", StaticFiles(directory=_IMAGES_DIR), name="imagens")


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


def _generate(payload: VehiclePayload, out_path: str) -> dict:
    """Gera o PNG em out_path. Devolve um dict com avisos (ex: falha a
    descarregar a foto), para o chamador decidir o que fazer com eles."""
    vehicle = payload.model_dump()
    photo_url = vehicle.pop("photo_url", None)

    tmp_photo_path = None
    warnings = {}
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
            # honesto que o motor ja tem, e regista o motivo no aviso
            # devolvido para facilitar debug no N8N.
            tmp_photo_path = None
            warnings["photo_error"] = str(e)

    vehicle["photo_path"] = tmp_photo_path

    try:
        render_post(vehicle, out_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha a gerar o post: {e}")
    finally:
        if tmp_photo_path and os.path.exists(tmp_photo_path):
            os.remove(tmp_photo_path)

    return warnings


@app.post("/gerar-post")
def gerar_post(payload: VehiclePayload):
    out_path = os.path.join(tempfile.gettempdir(), f"post_{uuid.uuid4()}.png")
    warnings = _generate(payload, out_path)

    with open(out_path, "rb") as f:
        png_bytes = f.read()
    os.remove(out_path)

    headers = {}
    if warnings.get("photo_error"):
        headers["X-Photo-Warning"] = warnings["photo_error"][:200]

    return Response(content=png_bytes, media_type="image/png", headers=headers)


@app.post("/gerar-post-url")
def gerar_post_url(payload: VehiclePayload, request: Request):
    filename = f"{uuid.uuid4()}.png"
    out_path = os.path.join(_IMAGES_DIR, filename)
    warnings = _generate(payload, out_path)

    base_url = str(request.base_url).rstrip("/")
    return {
        "image_url": f"{base_url}/imagens/{filename}",
        "warnings": warnings,
    }
