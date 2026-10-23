"""Tabelas de referência: itens (id numérico -> código e nome em PT-BR) e locais.

Gera um cache compacto em dados/itens_cache.json a partir do items.json do
ao-bin-dumps (24 MB), para o coletor e o servidor não precisarem lê-lo inteiro.
"""
import json
import re
import urllib.request

from config import PASTA_DADOS

URL_ITEMS = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json"
URL_WORLD = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/world.json"

ARQ_ITEMS = PASTA_DADOS / "items.json"
ARQ_WORLD = PASTA_DADOS / "world.json"
ARQ_CACHE = PASTA_DADOS / "itens_cache.json"

# Mercados principais (LocationId -> nome exibido)
CIDADES = {
    7: "Thetford",
    1002: "Lymhurst",
    2004: "Bridgewatch",
    3005: "Caerleon",
    3008: "Martlock",
    4002: "Fort Sterling",
    5003: "Brecilien",
    3003: "Mercado Negro",
}

# Portais das cidades vendem no mesmo mercado da cidade
PORTAIS = {301: 7, 1301: 1002, 2301: 2004, 3301: 3008, 4301: 4002}


def _baixar(url, destino):
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    print(f"Baixando {url} ...")
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=120) as r:
        destino.write_bytes(r.read())


def _tier(codigo):
    m = re.match(r"T(\d)_", codigo)
    return int(m.group(1)) if m else 0


def _encantamento(codigo):
    m = re.search(r"@(\d)$", codigo)
    return int(m.group(1)) if m else 0


def gerar_cache(forcar_download=False):
    if forcar_download or not ARQ_ITEMS.exists():
        _baixar(URL_ITEMS, ARQ_ITEMS)
    if forcar_download or not ARQ_WORLD.exists():
        _baixar(URL_WORLD, ARQ_WORLD)

    itens = {}
    for i in json.loads(ARQ_ITEMS.read_text(encoding="utf-8")):
        codigo = i.get("UniqueName")
        if not codigo or not i.get("Index"):
            continue
        nomes = i.get("LocalizedNames") or {}
        nome = nomes.get("PT-BR") or nomes.get("EN-US")
        if not nome and "@" in codigo:
            nome = itens.get(codigo.split("@")[0], {}).get("nome")
        itens[codigo] = {
            "idx": int(i["Index"]),
            "nome": nome,
            "tier": _tier(codigo),
            "enc": _encantamento(codigo),
        }
    for codigo, item in itens.items():
        if not item["nome"]:
            base = itens.get(codigo.split("@")[0], {}).get("nome")
            item["nome"] = base or codigo

    locais = {}
    for w in json.loads(ARQ_WORLD.read_text(encoding="utf-8")):
        try:
            locais[int(w["Index"])] = w["UniqueName"].replace(" Market", "")
        except (KeyError, ValueError):
            continue
    locais.update({k: v for k, v in CIDADES.items()})

    ARQ_CACHE.write_text(
        json.dumps({"itens": itens, "locais": locais}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Cache gerado: {len(itens)} itens, {len(locais)} locais")


class Referencia:
    """Consulta rápida de itens e locais."""

    def __init__(self):
        if not ARQ_CACHE.exists():
            gerar_cache()
        dados = json.loads(ARQ_CACHE.read_text(encoding="utf-8"))
        self.itens = dados["itens"]
        self.locais = {int(k): v for k, v in dados["locais"].items()}
        self.por_indice = {v["idx"]: k for k, v in self.itens.items()}

    def codigo_por_indice(self, idx):
        return self.por_indice.get(idx)

    def local_normalizado(self, loc_id):
        """Junta portais com a cidade e devolve (id, nome)."""
        loc_id = PORTAIS.get(loc_id, loc_id)
        return loc_id, self.locais.get(loc_id, str(loc_id))

    def info(self, codigo):
        return self.itens.get(codigo) or {
            "nome": codigo, "tier": _tier(codigo), "enc": _encantamento(codigo)
        }


if __name__ == "__main__":
    gerar_cache(forcar_download=True)
