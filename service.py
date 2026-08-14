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
import hashlib
import hmac
import io
import json
import os
import secrets
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generate_post import render_post
from extract_onepilot_inventory import (
    extract_vehicles_from_html,
    fetch_live_html,
    passes_filters,
    to_post_vehicle,
)

app = FastAPI(title="Fenrion F2Car Post Generator")

# Pasta onde ficam as imagens geradas por /gerar-post-url, servidas
# estaticamente em /imagens/<ficheiro>.png. Vive no disco efemero da
# instancia -- suficiente porque a Instagram Graph API vai buscar a
# imagem segundos depois de ser criada (nao precisa de durar para sempre).
_IMAGES_DIR = os.path.join(tempfile.gettempdir(), "fenrion_imagens")
os.makedirs(_IMAGES_DIR, exist_ok=True)
app.mount("/imagens", StaticFiles(directory=_IMAGES_DIR), name="imagens")

HERE = os.path.dirname(os.path.abspath(__file__))

# Registo de clientes conhecidos -- ponto unico a estender quando houver
# mais stands a usar o painel de criterios. client_id e a chave usada em
# todos os endpoints /criterios, /viaturas e /preview-imagens.
CLIENTS = {
    "f2car": {"name": "F2Car", "inventory_url": "https://f2car.com/viaturas"},
}


# ---------------------------------------------------------------- Postgres
# Guarda os criterios de cada cliente (marca, preco, combustivel, desconto
# minimo, etc.) definidos no painel. DATABASE_URL vem da instancia Postgres
# da Render (fenrion-clientes-db) -- tem de ser ligada manualmente como
# variavel de ambiente do servico (ver painel da Render > Environment).
def _db_conn():
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL nao configurada neste servico -- liga a base de dados fenrion-clientes-db nas Environment Variables da Render.",
        )
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


@app.on_event("startup")
def _ensure_schema():
    # Nao falha o arranque do servico se a BD ainda nao estiver ligada --
    # so regista o aviso, para o resto da API (geracao de posts) continuar
    # a funcionar mesmo sem DATABASE_URL configurada.
    try:
        conn = _db_conn()
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS client_criteria (
                    client_id TEXT PRIMARY KEY,
                    criteria JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS client_templates (
                    client_id TEXT PRIMARY KEY,
                    config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS client_auth (
                    client_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS post_log (
                    id SERIAL PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    vehicle_model TEXT,
                    price TEXT,
                    old_price TEXT,
                    imagem BYTEA,
                    source TEXT NOT NULL DEFAULT 'preview',
                    criteria_snapshot JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS post_log_client_created_idx ON post_log (client_id, created_at DESC);")
        conn.close()
    except HTTPException:
        print("[startup] DATABASE_URL nao definida -- /criterios, /template, /demo-auth e /post-log ficarao indisponiveis ate ligares a BD.")
    except Exception as e:
        print(f"[startup] Nao consegui preparar as tabelas: {e}")


# ---------------------------------------------------------------- Password da demo / sessao do cliente
# A demo publica (/demo/<client_id>) fica atras de uma password por cliente.
# Nao e seguranca de nivel bancario (os endpoints de leitura por baixo
# continuam acessiveis a quem souber a forma da API) -- e a barreira de
# entrada normal para uma demo profissional partilhada por link: sem a
# password certa nao se ve conteudo nem se consegue guardar criterios.


def _get_secret_key() -> str:
    """Chave persistente (guardada na BD) usada para assinar os tokens de
    sessao -- gerada uma unica vez, para nao invalidar todas as sessoes
    ativas sempre que o servico reinicia ou faz redeploy."""
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key = 'secret_key'")
            row = cur.fetchone()
            if row:
                return row[0]
            new_key = secrets.token_hex(32)
            cur.execute(
                "INSERT INTO app_config (key, value) VALUES ('secret_key', %s) ON CONFLICT (key) DO NOTHING",
                (new_key,),
            )
        conn2 = _db_conn()
        try:
            with conn2, conn2.cursor() as cur2:
                cur2.execute("SELECT value FROM app_config WHERE key = 'secret_key'")
                return cur2.fetchone()[0]
        finally:
            conn2.close()
    finally:
        conn.close()


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex() + ":" + digest.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
        return hmac.compare_digest(expected.hex(), digest_hex)
    except Exception:
        return False


def _make_token(client_id: str, days_valid: int = 60) -> str:
    secret = _get_secret_key()
    expiry = int(time.time()) + days_valid * 86400
    msg = f"{client_id}:{expiry}"
    sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{client_id}.{expiry}.{sig}"


def _verify_token(client_id: str, token: str) -> bool:
    if not token:
        return False
    try:
        tok_client, expiry_str, sig = token.split(".")
        if tok_client != client_id:
            return False
        expiry = int(expiry_str)
        if expiry < time.time():
            return False
        secret = _get_secret_key()
        msg = f"{client_id}:{expiry}"
        expected_sig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected_sig, sig)
    except Exception:
        return False


def _require_client_token(client_id: str, request: Request):
    token = request.headers.get("x-demo-token", "")
    if not _verify_token(client_id, token):
        raise HTTPException(status_code=401, detail="Sessao invalida ou expirada -- introduz a password outra vez.")


# Chaves aceites num template customizado (upload no painel). logo_asset fica
# de fora de proposito -- ver nota em generate_post.merge_config.
_TEMPLATE_ALLOWED_KEYS = {"canvas", "colors", "layout", "fixed_text"}


def _get_client_template(client_id: str):
    """Devolve o template customizado guardado para este cliente, ou None se
    nao houver BD ligada ou nao houver nenhum guardado -- em qualquer dos
    casos o motor cai no template_config.json por omissao (nao propaga erro,
    a pre-visualizacao nao deve falhar so por causa disto)."""
    try:
        conn = _db_conn()
    except HTTPException:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT config FROM client_templates WHERE client_id = %s", (client_id,))
            row = cur.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        return None
    finally:
        conn.close()


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


def _generate(payload: VehiclePayload, out_path: str, config_overrides: dict | None = None) -> dict:
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
        render_post(vehicle, out_path, config_overrides=config_overrides)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha a gerar o post: {e}")
    finally:
        if tmp_photo_path and os.path.exists(tmp_photo_path):
            os.remove(tmp_photo_path)

    return warnings


@app.post("/gerar-post")
def gerar_post(payload: VehiclePayload, client_id: str | None = None):
    # Gera sempre em memoria (BytesIO), nunca em disco -- evita por completo
    # a classe de erro "FileNotFoundError" observada no disco efemero da
    # Render quando se grava e reabre o ficheiro na mesma request.
    # client_id opcional: se o N8N o passar, usa o template customizado
    # desse cliente (se houver); sem ele, usa sempre o template por omissao.
    config_overrides = _get_client_template(client_id) if client_id else None
    buf = io.BytesIO()
    warnings = _generate(payload, buf, config_overrides=config_overrides)
    buf.seek(0)
    png_bytes = buf.read()

    headers = {}
    if warnings.get("photo_error"):
        headers["X-Photo-Warning"] = warnings["photo_error"][:200]

    return Response(content=png_bytes, media_type="image/png", headers=headers)


@app.post("/gerar-post-url")
def gerar_post_url(payload: VehiclePayload, request: Request, client_id: str | None = None):
    config_overrides = _get_client_template(client_id) if client_id else None
    filename = f"{uuid.uuid4()}.png"
    out_path = os.path.join(_IMAGES_DIR, filename)
    warnings = _generate(payload, out_path, config_overrides=config_overrides)

    base_url = str(request.base_url).rstrip("/")
    return {
        "image_url": f"{base_url}/imagens/{filename}",
        "warnings": warnings,
    }


# ============================================================
# Painel de criterios (uso interno -- Henrique, durante a chamada de venda)
# ============================================================
#
# Fluxo: o painel HTML (/painel) permite escolher um cliente, ajustar
# criterios (marca, gama de preco, combustivel, desconto minimo, dias sem
# publicar) e ver de imediato quais as viaturas do inventario ao vivo que
# passariam no filtro -- com o post ja gerado, tal como ficaria no
# Instagram. So depois de validado e que os criterios ficam guardados
# (POST /criterios/<cliente>), para o motor de publicacao os poder
# respeitar no futuro.


@app.get("/clientes")
def listar_clientes():
    return CLIENTS


@app.get("/criterios/{client_id}")
def get_criterios(client_id: str):
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT criteria, updated_at FROM client_criteria WHERE client_id = %s", (client_id,))
            row = cur.fetchone()
        if not row:
            return {"client_id": client_id, "criteria": {}, "updated_at": None}
        return {"client_id": client_id, "criteria": row[0], "updated_at": row[1].isoformat()}
    finally:
        conn.close()


@app.post("/criterios/{client_id}")
def guardar_criterios(client_id: str, criteria: dict = Body(...)):
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO client_criteria (client_id, criteria, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (client_id) DO UPDATE SET criteria = EXCLUDED.criteria, updated_at = now()
                """,
                (client_id, json.dumps(criteria)),
            )
        return {"ok": True, "client_id": client_id}
    finally:
        conn.close()


@app.post("/demo-auth/{client_id}")
def demo_auth(client_id: str, payload: dict = Body(...)):
    """Login da demo publica -- verifica a password do cliente e devolve um
    token de sessao (valido 60 dias) para o browser guardar e reutilizar nos
    pedidos seguintes (cabecalho X-Demo-Token)."""
    if client_id not in CLIENTS:
        raise HTTPException(status_code=404, detail="Cliente desconhecido.")
    password = str(payload.get("password", ""))
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT password_hash FROM client_auth WHERE client_id = %s", (client_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not _verify_password(password, row[0]):
        raise HTTPException(status_code=401, detail="Password incorreta.")
    return {"ok": True, "token": _make_token(client_id)}


@app.post("/demo/{client_id}/criterios")
def guardar_criterios_cliente(client_id: str, request: Request, criteria: dict = Body(...)):
    """Mesma logica de POST /criterios/<client_id>, mas para a vista
    'Personalizar' da demo publica -- exige uma sessao valida (password),
    ao contrario do endpoint interno usado pelo painel."""
    _require_client_token(client_id, request)
    return guardar_criterios(client_id, criteria)


def _fetch_filtered_vehicles(client_id: str, criteria_json: str):
    client = CLIENTS.get(client_id)
    if not client:
        raise HTTPException(status_code=404, detail=f"Cliente '{client_id}' desconhecido. Ver /clientes.")
    try:
        criteria = json.loads(criteria_json) if criteria_json else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Parametro 'criteria' tem de ser JSON valido.")

    try:
        html = fetch_live_html(client["inventory_url"])
        raw_vehicles = extract_vehicles_from_html(html)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Falha a ler o inventario ao vivo de {client['inventory_url']}: {e}")

    matched = [v for v in raw_vehicles if passes_filters(v, criteria)]
    return client, criteria, matched


@app.get("/viaturas/{client_id}")
def get_viaturas(client_id: str, criteria: str = "{}"):
    client, crit, matched = _fetch_filtered_vehicles(client_id, criteria)
    vehicles = [to_post_vehicle(v) for v in matched]
    for v in vehicles:
        v.pop("photo_path", None)
    return {
        "client_id": client_id,
        "total_no_inventario": None,
        "total_disponivel": len(matched),
        "criteria_aplicados": crit,
        "viaturas": vehicles,
    }


def _log_post(client_id: str, vehicle: dict, image_bytes: bytes, source: str, criteria: dict):
    """Regista uma entrada no arquivo historico de posts (ver /post-log).
    Falha em silencio (so imprime um aviso) para nunca deitar abaixo uma
    pre-visualizacao so por causa do registo -- e um extra, nao o essencial."""
    try:
        import psycopg2
        conn = _db_conn()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO post_log (client_id, vehicle_model, price, old_price, imagem, source, criteria_snapshot, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                    """,
                    (
                        client_id,
                        vehicle.get("model"),
                        vehicle.get("price"),
                        vehicle.get("old_price"),
                        psycopg2.Binary(image_bytes),
                        source,
                        json.dumps(criteria),
                    ),
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[post_log] falha a registar post de {client_id}: {e}")


def _generate_one(base_url, vehicle, config_overrides, client_id=None, registar=False, criteria=None):
    photo_url = vehicle.pop("photo_url", None)
    payload = VehiclePayload(**{k: v for k, v in vehicle.items() if k in VehiclePayload.model_fields}, photo_url=photo_url)
    filename = f"{uuid.uuid4()}.png"
    out_path = os.path.join(_IMAGES_DIR, filename)
    try:
        warnings = _generate(payload, out_path, config_overrides=config_overrides)
        image_url = f"{base_url}/imagens/{filename}"
        if registar and client_id:
            try:
                with open(out_path, "rb") as f:
                    _log_post(client_id, vehicle, f.read(), "preview", criteria or {})
            except Exception as e:
                print(f"[post_log] falha a ler imagem gerada para registo: {e}")
    except HTTPException as e:
        warnings = {"error": str(e.detail)}
        image_url = None
    return {"vehicle": vehicle, "image_url": image_url, "warnings": warnings}


@app.get("/preview-imagens/{client_id}")
def get_preview_imagens(client_id: str, request: Request, criteria: str = "{}", limit: int = 8, registar: bool = False):
    client, crit, matched = _fetch_filtered_vehicles(client_id, criteria)
    matched = matched[: max(1, min(limit, 20))]

    # Usa sempre o template customizado deste cliente, se houver um guardado
    # (ver /template/<client_id>) -- e assim que uma troca de template no
    # painel se reflete de imediato na pre-visualizacao e na demo publica.
    config_overrides = _get_client_template(client_id)
    base_url = str(request.base_url).rstrip("/")
    vehicles = [to_post_vehicle(raw) for raw in matched]

    # Gera as imagens em paralelo (thread pool) em vez de uma a uma --
    # cada geracao e sobretudo I/O (download da foto) + CPU curta (PIL),
    # por isso threads ja ajudam bastante. Sem isto, 8-12 viaturas num
    # unico pedido HTTP podiam facilmente passar de 60-90s e ficar
    # vulneraveis a timeout (foi o que aconteceu na demo em rede movel).
    results_by_index = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(_generate_one, base_url, v, config_overrides, client_id, registar, crit): i
            for i, v in enumerate(vehicles)
        }
        for future in as_completed(futures):
            results_by_index[futures[future]] = future.result()
    results = [results_by_index[i] for i in range(len(vehicles))]

    return {"client_id": client_id, "criteria_aplicados": crit, "resultados": results}


@app.get("/template/{client_id}")
def get_template(client_id: str):
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT config, updated_at FROM client_templates WHERE client_id = %s", (client_id,))
            row = cur.fetchone()
        if not row:
            return {"client_id": client_id, "config": {}, "updated_at": None}
        return {"client_id": client_id, "config": row[0], "updated_at": row[1].isoformat()}
    finally:
        conn.close()


@app.post("/template/{client_id}")
def guardar_template(client_id: str, config: dict = Body(...)):
    unknown_keys = set(config.keys()) - _TEMPLATE_ALLOWED_KEYS
    if unknown_keys:
        raise HTTPException(
            status_code=400,
            detail=f"Chaves nao suportadas no template: {sorted(unknown_keys)}. Permitidas: {sorted(_TEMPLATE_ALLOWED_KEYS)} (o logotipo ainda nao e substituivel por aqui).",
        )
    # Validacao minima: gera um post de teste com este template antes de o
    # guardar, para nao deixares um template partido guardado sem saberes.
    try:
        buf = io.BytesIO()
        sample = VehiclePayload(model="Modelo de Teste", price="10.000€")
        _generate(sample, buf, config_overrides=config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Este template nao gera um post valido: {e}")

    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO client_templates (client_id, config, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (client_id) DO UPDATE SET config = EXCLUDED.config, updated_at = now()
                """,
                (client_id, json.dumps(config)),
            )
        return {"ok": True, "client_id": client_id}
    finally:
        conn.close()


@app.delete("/template/{client_id}")
def repor_template(client_id: str):
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM client_templates WHERE client_id = %s", (client_id,))
        return {"ok": True, "client_id": client_id, "reposto": "template por omissao"}
    finally:
        conn.close()


# ============================================================
# Historico de posts (arquivo consultavel por Henrique e pelo cliente)
# ============================================================
#
# Por agora regista pre-visualizacoes (source='preview'), ligado a partir da
# demo -- simula o fluxo antes de termos o acesso a conta de Instagram do
# cliente. Quando o workflow N8N passar a publicar de verdade, a mesma
# tabela passa a receber entradas source='published' (endpoint a construir
# nessa altura), e o Henrique disse que quer que so essas contem para o
# historico "oficial" a partir daí.


@app.get("/post-log/{client_id}")
def listar_log(client_id: str, desde: str | None = None, ate: str | None = None, fonte: str | None = None, limit: int = 200):
    conn = _db_conn()
    try:
        query = "SELECT id, vehicle_model, price, old_price, source, created_at FROM post_log WHERE client_id = %s"
        params = [client_id]
        if desde:
            query += " AND created_at >= %s"
            params.append(desde)
        if ate:
            query += " AND created_at <= %s"
            params.append(ate)
        if fonte:
            query += " AND source = %s"
            params.append(fonte)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(1, min(limit, 500)))
        with conn, conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    return {
        "client_id": client_id,
        "entradas": [
            {
                "id": r[0],
                "modelo": r[1],
                "preco": r[2],
                "preco_antes": r[3],
                "fonte": r[4],
                "criado_em": r[5].isoformat(),
            }
            for r in rows
        ],
    }


@app.get("/post-log/{client_id}/{log_id}/imagem")
def imagem_log(client_id: str, log_id: int):
    conn = _db_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT imagem FROM post_log WHERE client_id = %s AND id = %s", (client_id, log_id))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="Imagem nao encontrada.")
    return Response(content=bytes(row[0]), media_type="image/png")


@app.get("/painel", response_class=FileResponse)
def painel():
    path = os.path.join(HERE, "painel.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="painel.html nao encontrado no servico.")
    return FileResponse(path, media_type="text/html")


@app.get("/demo/{client_id}", response_class=FileResponse)
def demo(client_id: str):
    # Pagina de demo para partilhar com o cliente/prospect -- so leitura,
    # sem nenhum controlo de filtro visivel. O client_id na URL identifica
    # o cliente (ex: /demo/f2car); o proprio demo.html le-o do path e usa
    # os criterios ja guardados em /criterios/<client_id> (se existirem).
    path = os.path.join(HERE, "demo.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="demo.html nao encontrado no servico.")
    return FileResponse(path, media_type="text/html")
