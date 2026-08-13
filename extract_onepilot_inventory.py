"""
Conector Fenrion -- extrai o inventario real de um site construido em cima
do OnePilot (confirmado: <meta name="author" content="onepilot.app">).

Como funciona: o Next.js (a framework do site) embebe os dados de TODAS as
viaturas, ja estruturados, dentro de uma tag <script> da propria pagina
(o mecanismo de "React Server Components streaming"). Nao e preciso
nenhuma API nem browser sem cabeca -- um simples pedido HTTP normal a
pagina de viaturas ja traz os dados no HTML de resposta.

Como este payload vem por baixo do formato interno do Next.js
("self.__next_f.push([1, "f:...."])"), o truque e: 1) isolar esse array
JS (que e JSON valido), 2) deixar o proprio parser de JSON do Python
desescapar a string interna por nos (muito mais robusto que tentar
desescapar "\\"" a mao), 3) tirar o prefixo "f:" e voltar a fazer parse,
4) percorrer a arvore ate encontrar a lista de viaturas.

Isto generaliza, em princípio, para qualquer outro stand que use o
OnePilot -- e a mesma framework a gerar o mesmo tipo de payload.
"""
import json


def fetch_live_html(url: str, timeout: int = 20) -> str:
    """Vai buscar o HTML ao vivo da pagina de viaturas de um stand OnePilot.
    Usado pelo painel de criterios para pre-visualizar o inventario atual
    sem depender de um ficheiro carregado manualmente."""
    import requests
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def compute_pricing(v: dict):
    """Devolve (preco_atual, preco_antes|None, desconto_pct) a partir do
    registo bruto do OnePilot -- a mesma logica de campanha usada em
    to_post_vehicle, mas so com os numeros (sem formatacao), para ser
    reutilizavel tanto na geracao do post como na filtragem de criterios."""
    campaign = v.get("campaign")
    if campaign and campaign.get("value"):
        current = campaign["value"]
        old = v.get("price")
    else:
        current = v.get("price")
        old = None
    discount_pct = 0.0
    if old and current and old > 0:
        discount_pct = round((old - current) / old * 100, 1)
    return current, old, discount_pct


def passes_filters(v: dict, criteria: dict) -> bool:
    """Aplica os criterios definidos no painel (marca, preco, combustivel,
    desconto minimo, so-com-campanha) a um registo bruto do OnePilot.
    Criterios ausentes/vazios sao ignorados (nao filtram nada)."""
    if v.get("status") != "available":
        return False

    current, old, discount_pct = compute_pricing(v)
    if current is None:
        return False

    brand = (v.get("brand") or "").strip().lower()
    brands_include = [b.strip().lower() for b in criteria.get("brands_include", []) if b.strip()]
    brands_exclude = [b.strip().lower() for b in criteria.get("brands_exclude", []) if b.strip()]
    if brands_include and brand not in brands_include:
        return False
    if brands_exclude and brand in brands_exclude:
        return False

    price_min = criteria.get("price_min")
    price_max = criteria.get("price_max")
    if price_min is not None and current < price_min:
        return False
    if price_max is not None and current > price_max:
        return False

    fuel = (v.get("fuel") or "").strip().lower()
    fuels = [f.strip().lower() for f in criteria.get("fuels", []) if f.strip()]
    if fuels and fuel not in fuels:
        return False

    if criteria.get("only_with_campaign") and not old:
        return False

    min_discount_pct = criteria.get("min_discount_pct")
    if min_discount_pct is not None and discount_pct < min_discount_pct:
        return False

    return True


def extract_vehicles_from_html(html: str):
    marker = html.find('\\"data\\":[')
    if marker == -1:
        raise ValueError("Nao encontrei o payload de dados nesta pagina -- pode nao ser uma pagina de listagem OnePilot.")

    push_start = html.rfind('self.__next_f.push([1,"', 0, marker)
    arg_start = html.find('[1,"', push_start)
    end_marker = html.find('"])</script>', arg_start)
    raw_list_literal = html[arg_start:end_marker + 2]

    parsed = json.loads(raw_list_literal)   # ["1, "f:...."] -> desescapa a string interna sozinho
    inner_str = parsed[1]
    assert inner_str.startswith("f:")
    tree = json.loads(inner_str[2:])

    def find_data(node):
        if isinstance(node, dict):
            if (
                "data" in node
                and isinstance(node["data"], list)
                and node["data"]
                and isinstance(node["data"][0], dict)
                and "brand" in node["data"][0]
            ):
                return node["data"]
            for v in node.values():
                r = find_data(v)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = find_data(v)
                if r is not None:
                    return r
        return None

    vehicles = find_data(tree)
    if vehicles is None:
        raise ValueError("Payload encontrado, mas nao consegui localizar a lista de viaturas dentro dele.")
    return vehicles


def to_post_vehicle(v: dict, photo_path=None) -> dict:
    """Converte um registo bruto do OnePilot no formato que o gerador de posts espera.

    Preco "antes/depois": quando a viatura tem uma campanha ativa no OnePilot
    (campo `campaign`), o `price` normal e sempre o preco ANTES e
    `campaign.value` e o preco promocional atual (confirmado por amostragem:
    campaign.value < price em 100% dos 23 casos com campanha do inventario
    F2Car). Portanto isto e automatico -- nao precisa de input manual.
    """
    gearbox_map = {"Automática": "Auto", "Manual": "Manual"}
    title = f"{v['brand']} {v['model']}"
    campaign = v.get("campaign")
    # Viaturas reservadas por vezes vem sem preco publicado (None) -- nao ha
    # nada a mostrar nesse caso, o post nao deve ser gerado para elas mesmo.
    if campaign and campaign.get("value"):
        current_price = campaign["value"]
        old_price = v.get("price")
    else:
        current_price = v.get("price")
        old_price = None
    return {
        "photo_path": photo_path,
        "photo_url": (v.get("thumbnail", {}).get("srcSet", {}) or {}).get("xl"),
        "model": title,
        "fuel": v.get("fuel", "-"),
        "power": f"{v['hp']}cv" if v.get("hp") else "-",
        "km": f"{v['kms']:,}".replace(",", ".") + "kms",
        "year": str(v.get("year", "-")),
        "gearbox": gearbox_map.get(v.get("gearbox"), v.get("gearbox", "-")),
        "condition": "NACIONAL",
        "price": (f"{current_price:,}".replace(",", ".") + "€") if current_price else None,
        "old_price": (f"{old_price:,}".replace(",", ".") + "€") if old_price else None,
        "status": v.get("status"),
        "id": v.get("id"),
        "slug": v.get("slug"),
    }


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "/sessions/compassionate-serene-gauss/mnt/uploads/Viaturas | F2CAR - STAND E OFICINA PREMIUM.html"
    with open(path, encoding="utf-8", errors="replace") as f:
        html = f.read()
    vehicles = extract_vehicles_from_html(html)
    available = [v for v in vehicles if v.get("status") == "available"]
    print(f"Total no payload: {len(vehicles)}  |  Disponiveis (nao reservados): {len(available)}")
    for v in available[:3]:
        print(" -", to_post_vehicle(v))
