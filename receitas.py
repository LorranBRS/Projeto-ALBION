"""Receitas de fabricação/refino, valor dos itens e bônus de produção.

Fonte: arquivos brutos do ao-bin-dumps (items.json, craftingmodifiers.json,
festivities.json). Gera dados/receitas_cache.json com o necessário para o site.

Uso:
    python receitas.py        # baixa os arquivos e gera o cache
"""
import json
import urllib.request

from config import PASTA_DADOS

BASE_URL = "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/"
PASTA_BRUTO = PASTA_DADOS / "bruto"
ARQ_CACHE = PASTA_DADOS / "receitas_cache.json"

# Nutrição gasta por fabricação = valor do item x este fator
FATOR_NUTRICAO = 0.1125
# Bônus de produção do foco
BONUS_FOCO = 0.59

# clusterid do jogo -> (id do mercado, nome)
CLUSTERS = {
    "0000": (7, "Thetford"),
    "1000": (1002, "Lymhurst"),
    "2000": (2004, "Bridgewatch"),
    "3004": (3008, "Martlock"),
    "4000": (4002, "Fort Sterling"),
    "3003": (3005, "Caerleon"),
    "5000": (5003, "Brecilien"),
    "4300": (None, "Descanso de Artur"),
    "1012": (None, "Descanso de Merlin"),
    "0008": (None, "Descanso de Morgana"),
}

# Categorias de fabricação/refino (craftingcategory) em português
CATEGORIAS = {
    "sword": "Espadas", "axe": "Machados", "mace": "Maças", "hammer": "Martelos",
    "crossbow": "Bestas", "bow": "Arcos", "spear": "Lanças", "dagger": "Adagas",
    "quarterstaff": "Bordões", "knuckles": "Manoplas de guerra",
    "shapeshifterstaff": "Cajados metamorfos", "naturestaff": "Cajados da natureza",
    "firestaff": "Cajados de fogo", "froststaff": "Cajados de gelo",
    "arcanestaff": "Cajados arcanos", "holystaff": "Cajados sagrados",
    "cursestaff": "Cajados amaldiçoados", "offhand": "Mão secundária",
    "plate_helmet": "Elmos de placa", "plate_armor": "Armaduras de placa",
    "plate_shoes": "Botas de placa", "leather_helmet": "Capuzes de couro",
    "leather_armor": "Casacos de couro", "leather_shoes": "Sapatos de couro",
    "cloth_helmet": "Capuzes de tecido", "cloth_armor": "Robes de tecido",
    "cloth_shoes": "Sandálias de tecido", "gatherergear": "Equipamento de coleta",
    "tools": "Ferramentas", "food": "Comida", "potion": "Poções", "bag": "Bolsas",
    "cape": "Capas", "ore": "Barras de metal (refino de minério)",
    "hide": "Couro (refino de pele)", "fiber": "Tecido (refino de fibra)",
    "wood": "Tábuas (refino de madeira)", "rock": "Blocos de pedra (refino de pedra)",
    "meat_pig": "Carne de porco", "meat_goose": "Carne de ganso",
    "meat_goat": "Carne de cabra", "meat_cow": "Carne de vaca",
    "meat_chicken": "Carne de galinha", "meat_sheep": "Carne de ovelha",
}
REFINO = {"ore", "hide", "fiber", "wood", "rock"}


def _lista(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _id_mercado(nome, nivel):
    nivel = int(nivel or 0)
    return f"{nome}@{nivel}" if nivel > 0 else nome


def _baixar(nome):
    PASTA_BRUTO.mkdir(parents=True, exist_ok=True)
    destino = PASTA_BRUTO / nome
    print(f"Baixando {nome} ...")
    with urllib.request.urlopen(BASE_URL + nome, timeout=180) as r:
        destino.write_bytes(r.read())
    return destino


def _receita(req):
    """Converte um craftingrequirements do jogo no formato do site."""
    opcoes = _lista(req)
    if not opcoes:
        return None
    # A primeira opção é a receita padrão (as outras usam tokens de facção etc.)
    r = opcoes[0]
    recursos = []
    for c in _lista(r.get("craftresource")):
        recursos.append({
            "id": _id_mercado(c["@uniquename"], c.get("@enchantmentlevel")),
            "qtd": int(c["@count"]),
            "retorna": c.get("@maxreturnamount") != "0",
        })
    if not recursos:
        return None
    return {
        "recursos": recursos,
        "prata": int(float(r.get("@silver", 0) or 0)),
        "qtd_fabricada": int(r.get("@amountcrafted", 1) or 1),
    }


def gerar_cache(baixar=True):
    arquivos = ["items.json", "craftingmodifiers.json", "festivities.json"]
    if baixar or not all((PASTA_BRUTO / a).exists() for a in arquivos):
        for a in arquivos:
            _baixar(a)

    itens = json.loads((PASTA_BRUTO / "items.json").read_text(encoding="utf-8"))["items"]
    receitas, valores = {}, {}
    for grupo in itens.values():
        for x in _lista(grupo):
            if not isinstance(x, dict) or "@uniquename" not in x:
                continue
            nome = x["@uniquename"]
            mid = _id_mercado(nome, x.get("@enchantmentlevel"))
            if x.get("@itemvalue"):
                valores[mid] = float(x["@itemvalue"])
            cat = x.get("@craftingcategory")
            rec = _receita(x.get("craftingrequirements"))
            if rec:
                receitas[mid] = {"cat": cat, **rec}
            # Equipamentos trazem as versões encantadas dentro do próprio item
            for e in _lista((x.get("enchantments") or {}).get("enchantment")):
                rec_e = _receita(e.get("craftingrequirements"))
                if rec_e:
                    receitas[_id_mercado(nome, e["@enchantmentlevel"])] = {"cat": cat, **rec_e}

    # Valor do item fabricado = soma dos valores dos recursos (é a base da nutrição)
    for mid, rec in receitas.items():
        if mid not in valores:
            v = sum(valores.get(r["id"], 0) * r["qtd"] for r in rec["recursos"])
            if v:
                valores[mid] = v
        rec["valor"] = valores.get(mid, 0)

    mods = json.loads((PASTA_BRUTO / "craftingmodifiers.json").read_text(
        encoding="utf-8"))["craftingmodifiers"]
    cidades = []
    for loc in _lista(mods.get("craftinglocation")):
        cluster = loc.get("@clusterid")
        if cluster not in CLUSTERS:
            continue  # biomas das Terras Distantes ficam de fora
        local, nome = CLUSTERS[cluster]
        cidades.append({
            "cluster": cluster, "local": local, "nome": nome,
            "base_refino": float(loc.get("refiningbonus", {}).get("@value", 0)),
            "base_craft": float(loc.get("craftingbonus", {}).get("@value", 0)),
            "mods": {m["@name"]: float(m["@value"])
                     for m in _lista(loc.get("craftingmodifier"))},
        })

    fest = json.loads((PASTA_BRUTO / "festivities.json").read_text(
        encoding="utf-8"))["festivities"]
    diario = {}
    for cat in _lista((fest.get("dailyevents") or {}).get("eventcategory")):
        for ev in _lista(cat.get("craftbonusevent")):
            diario[ev["@uniquename"]] = {"cat": ev["@category"],
                                         "valor": float(ev["@value"])}

    ARQ_CACHE.write_text(json.dumps({
        "receitas": receitas, "valores": valores, "cidades": cidades,
        "bonus_diario": diario,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"Cache de receitas: {len(receitas)} receitas, {len(cidades)} locais "
          f"de produção, {len(diario)} bônus diários possíveis")


class Receitas:
    def __init__(self):
        if not ARQ_CACHE.exists():
            gerar_cache(baixar=not (PASTA_BRUTO / "items.json").exists())
        d = json.loads(ARQ_CACHE.read_text(encoding="utf-8"))
        self.receitas = d["receitas"]
        self.valores = d["valores"]
        self.cidades = d["cidades"]
        self.bonus_diario = d["bonus_diario"]

    def bonus(self, cidade, cat, bonus_dia=0.0, foco=False):
        """Bônus de produção total (fração) de uma categoria numa cidade."""
        base = cidade["base_refino"] if cat in REFINO else cidade["base_craft"]
        total = base + cidade["mods"].get(cat, 0) + bonus_dia
        return total + (BONUS_FOCO if foco else 0)


def taxa_retorno(bonus):
    """Fração dos recursos devolvida, dado o bônus de produção."""
    return 1 - 1 / (1 + bonus)


if __name__ == "__main__":
    gerar_cache(baixar=False)
