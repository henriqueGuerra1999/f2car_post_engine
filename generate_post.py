"""
Motor de geracao de posts Instagram -- F2Car (Fenrion AI)
------------------------------------------------------------
Le a configuracao editavel (template_config.json) + os dados de um veiculo,
e gera a imagem do post pronta a publicar, seguindo o template real que a
F2Car ja usa todos os dias.

Uso:
    python3 generate_post.py

Para trocar o layout/cores: editar template_config.json, nao este ficheiro.
Para trocar o carro: editar o dicionario `vehicle` no fundo deste ficheiro
(ou, na versao ligada ao inventario real, isto vem do site/OnePilot).
"""
import json, os
from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "template_config.json"), encoding="utf-8"))

# Fontes: procura a copia local do .ttf em varios sitios plausiveis, para
# ser resiliente a como o repo foi organizado (upload manual pelo GitHub
# web UI, por exemplo, pode nao preservar a subpasta fonts/ e deixar os
# .ttf soltos na raiz do repo). So recorre ao path de sistema do sandbox
# se nao encontrar nenhuma copia local (ex: ambiente de desenvolvimento).
_FONT_DIR_CANDIDATES = [
    os.path.join(HERE, "fonts"),
    HERE,
    "/usr/share/fonts/truetype/google-fonts/",
]

def _resolve_font_dir():
    for candidate in _FONT_DIR_CANDIDATES:
        if os.path.isfile(os.path.join(candidate, "Poppins-Bold.ttf")):
            return candidate
    raise FileNotFoundError(
        "Nao encontrei Poppins-Bold.ttf em nenhum destes sitios: "
        + ", ".join(_FONT_DIR_CANDIDATES)
    )

FONT_DIR = _resolve_font_dir()

def F(weight, size):
    paths = {
        "bold": os.path.join(FONT_DIR, "Poppins-Bold.ttf"),
        "medium": os.path.join(FONT_DIR, "Poppins-Medium.ttf"),
        "bolditalic": os.path.join(FONT_DIR, "Poppins-BoldItalic.ttf"),
        "regular": os.path.join(FONT_DIR, "Poppins-Regular.ttf"),
    }
    return ImageFont.truetype(paths[weight], size)

def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def center_text(draw, cx, y, text, font, fill, tracking=0):
    if tracking:
        widths = [draw.textlength(ch, font=font) + tracking for ch in text]
        total = sum(widths) - tracking
        x = cx - total / 2
        for ch, wch in zip(text, widths):
            draw.text((x, y), ch, font=font, fill=fill)
            x += wch
        return
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)

def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)

def render_post(vehicle, out_path):
    """
    vehicle = {
      "photo_path": str|None,     # None -> usa placeholder honesto
      "model": "BMW M2",
      "fuel": "Gasolina", "power": "480cv", "km": "7.000kms", "year": "2025", "gearbox": "Auto",
      "condition": "NACIONAL",
      "price": "89.900€",
      "old_price": "99.000€" | None,
    }
    """
    W, H = CONFIG["canvas"]["width"], CONFIG["canvas"]["height"]
    C = {k: hex2rgb(v) for k, v in CONFIG["colors"].items()}
    photo_end = int(H * CONFIG["layout"]["photo_area_frac"])
    cream_end = int(H * CONFIG["layout"]["cream_panel_end_frac"])

    img = Image.new("RGB", (W, H), C["cream_panel"])
    draw = ImageDraw.Draw(img)

    # ---------------- zona da foto ----------------
    if vehicle.get("photo_path") and os.path.exists(vehicle["photo_path"]):
        photo = Image.open(vehicle["photo_path"]).convert("RGB")
        photo = ImageOps.fit(photo, (W, photo_end), method=Image.LANCZOS)
        img.paste(photo, (0, 0))
    else:
        # placeholder honesto -- nunca finge ter uma foto real que nao existe
        draw.rectangle([0, 0, W, photo_end], fill=C["photo_bg_dark"])
        ph_font = F("medium", 26)
        center_text(draw, W / 2, photo_end / 2 - 40, "FOTO REAL DO VEICULO", ph_font, (150, 150, 152))
        center_text(draw, W / 2, photo_end / 2, "(a ligar ao inventario real)", F("regular", 20), (120, 120, 122))

    # curva dourada de transicao entre a foto e o painel creme
    arc_h = 70
    draw.pieslice([-100, photo_end - arc_h, W + 100, photo_end + arc_h], 180, 360, fill=C["cream_panel"])
    draw.arc([-100, photo_end - arc_h, W + 100, photo_end + arc_h], 180, 360, fill=C["gold_tan"], width=6)

    # ---------------- etiqueta de preco ----------------
    tag_right = W - 60
