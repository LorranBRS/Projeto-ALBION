"""Servidor do site "Albion em Alta": API JSON + página estática.

Uso:
    uvicorn servidor:app --host 0.0.0.0 --port 8000
"""
import json
import math
import sqlite3
import time
import unicodedata
from collections import defaultdict

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import httpx
import numpy as np

import limpeza
import previsao
import receitas as receitas_mod
import config
from config import (BANCO, ESCALA_6H_7D, ESCALA_6H_28D, ESCALA_HORA, RAIZ,
                    REGIOES)
from itens import CIDADES, Referencia

def sem_acento(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn").lower()


ref = Referencia()
receitas = receitas_mod.Receitas()
for _codigo, _item in ref.itens.items():
    _item["busca"] = sem_acento(f"{_item['nome']} {_codigo}")

app = FastAPI(title="Albion em Alta")
app.add_middleware(GZipMiddleware, minimum_size=1000)

CACHE_SEGUNDOS = 60
_cache = {}

# Filtros mínimos para um item entrar no ranking (evita ruído de 1 venda)
MIN_VENDAS_24H = 10
MIN_VENDAS_DIA_7D = 3
JANELA_ORDENS_ATUAIS = 6 * 3600  # ordens vistas nas últimas 6h valem como "no mercado"


def conectar():
    con = sqlite3.connect(f"file:{BANCO}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def em_cache(chave, gerar):
    agora = time.time()
    if chave in _cache and agora - _cache[chave][0] < CACHE_SEGUNDOS:
        return _cache[chave][1]
    valor = gerar()
    _cache[chave] = (agora, valor)
    return valor


def validar_regiao(regiao):
    if regiao not in REGIOES:
        raise HTTPException(404, f"Região inválida. Use: {list(REGIOES)}")


def pct(atual, base):
    return round((atual / base - 1) * 100, 1) if base else None


# --- leitura do histórico já limpo ----------------------------------------
def historico_limpo(con, regiao, desde, escalas, escala_ref, filtros="", args=()):
    """Lê pontos de histórico e descarta picos fora do padrão.

    A referência de "preço normal" de cada (item, local, qualidade) é a mediana
    dos pontos da escala_ref; sem pontos suficientes nela, usa os da escala de
    cada ponto. Devolve ({combo: [(escala, ts, qtd, prata)]}, descartados).
    """
    combos = defaultdict(list)
    for item, loc, q, esc, ts, qtd, prata in con.execute(f"""
        SELECT item, local, qualidade, escala, ts, quantidade, prata FROM historico
        WHERE regiao=? AND ts>=? AND quantidade>0
          AND escala IN ({",".join(str(e) for e in escalas)}) {filtros}""",
            (regiao, desde, *args)):
        combos[(item, loc, q)].append((esc, ts, qtd, prata))

    limpos, descartados = {}, 0
    for combo, pontos in combos.items():
        precos_ref = [p / q for e, _, q, p in pontos if e == escala_ref]
        refe = limpeza.referencia(precos_ref)
        refs = {escala_ref: refe}
        for e in escalas:
            if e != escala_ref:
                refs[e] = refe or limpeza.referencia(
                    [p / q for e2, _, q, p in pontos if e2 == e])
        bons = [pt for pt in pontos
                if limpeza.dentro_do_padrao(pt[3] / pt[2], refs[pt[0]])]
        descartados += len(pontos) - len(bons)
        limpos[combo] = bons
    return limpos, descartados


# --- ranking -------------------------------------------------------------
def calcular_ranking(regiao, local=None):
    agora = int(time.time())
    d1, d8, h3 = agora - 86400, agora - 8 * 86400, agora - 3 * 3600
    filtro_local = "AND local=?" if local is not None else ""
    args_local = (local,) if local is not None else ()
    con = conectar()

    combos, descartados = historico_limpo(
        con, regiao, d8, (ESCALA_HORA, ESCALA_6H_7D), ESCALA_6H_7D,
        filtro_local, args_local)

    # Soma por item. O crescimento só compara combinações (item, local,
    # qualidade) que têm tanto o histórico por hora quanto o de 7 dias, para
    # não misturar coberturas diferentes.
    soma = defaultdict(lambda: defaultdict(float))
    precos_item = defaultdict(list)
    for (item, _, _), pontos in combos.items():
        v24 = p24 = v3h = v7 = p7 = 0
        ultima = 0
        for esc, ts, q, p in pontos:
            if esc == ESCALA_HORA and ts >= d1:
                v24 += q
                p24 += p
                ultima = max(ultima, ts + 3600)
                if ts >= h3:
                    v3h += q
            elif esc == ESCALA_6H_7D and ts < d1:
                v7 += q
                p7 += p
            if esc == ESCALA_6H_7D:
                precos_item[item].append(p / q)
        s = soma[item]
        s["v24"] += v24; s["p24"] += p24; s["v3h"] += v3h
        s["ultima"] = max(s["ultima"], ultima)
        if v24 and v7:
            s["v24c"] += v24; s["p24c"] += p24; s["v7c"] += v7; s["p7c"] += p7

    # Movimento de ordens: novas na última hora x média por hora das 24h anteriores
    inicio = con.execute("SELECT inicio FROM status WHERE regiao=?",
                         (regiao,)).fetchone()
    horas_coleta = (agora - inicio["inicio"]) / 3600 if inicio else 0
    ordens = {r["item"]: r for r in con.execute(f"""
        SELECT item,
            SUM(CASE WHEN primeira_vez>=:h1 THEN 1 ELSE 0 END) novas_1h,
            SUM(CASE WHEN primeira_vez<:h1 AND primeira_vez>=:h25 THEN 1 ELSE 0 END) novas_ant,
            MIN(CASE WHEN tipo='offer' AND ultima_vez>=:atual THEN preco END) menor_venda,
            MAX(CASE WHEN tipo='request' AND ultima_vez>=:atual THEN preco END) maior_compra
        FROM ordens WHERE regiao=:r AND ultima_vez>=:h25 {filtro_local.replace("?", ":loc")}
        GROUP BY item
    """, {"r": regiao, "h1": agora - 3600, "h25": agora - 25 * 3600,
          "atual": agora - JANELA_ORDENS_ATUAIS, "loc": local})}
    con.close()

    # Média por hora das horas anteriores (só depois de o coletor rodar >2h,
    # senão as ordens antigas "aparecem" como novas na primeira hora)
    horas_base = min(max(horas_coleta - 1, 0), 24)
    movimento_ok = horas_coleta >= 2

    itens, parados = [], 0
    for item, v in soma.items():
        if v["v24"] < MIN_VENDAS_24H:
            continue
        # Preço que não muda em 7 dias não diz nada sobre tendência
        if limpeza.preco_parado(precos_item[item]):
            parados += 1
            continue
        info = ref.info(item)
        media_dia_7d = v["v7c"] / 7 if v["v7c"] else 0
        preco_24 = v["p24"] / v["v24"]
        preco_24c = v["p24c"] / v["v24c"] if v["v24c"] else None
        preco_7 = v["p7c"] / v["v7c"] if v["v7c"] else None
        crescimento = (round(v["v24c"] / media_dia_7d, 2)
                       if media_dia_7d >= MIN_VENDAS_DIA_7D else None)
        o = ordens.get(item)
        ritmo = None
        if o and movimento_ok and o["novas_ant"]:
            ritmo = round(o["novas_1h"] / (o["novas_ant"] / horas_base), 2)
        itens.append({
            "id": item,
            "nome": info["nome"],
            "tier": info["tier"],
            "enc": info["enc"],
            "vendas_24h": int(v["v24"]),
            "vendas_3h": int(v["v3h"]),
            "prata_24h": int(v["p24"]),
            "preco_medio_24h": round(preco_24),
            "media_dia_7d": round(media_dia_7d, 1),
            "crescimento": crescimento,
            "var_preco": pct(preco_24c, preco_7) if preco_24c and preco_7 else None,
            "novas_ordens_1h": o["novas_1h"] if o else 0,
            "ritmo_ordens": ritmo,
            "menor_venda": o["menor_venda"] if o else None,
            "maior_compra": o["maior_compra"] if o else None,
            "ultima_venda": int(v["ultima"]),
        })

    # Índice "em alta": crescimento de vendas pesado pelo volume em prata
    for i in itens:
        c = i["crescimento"] or 0
        i["indice"] = round(c * (i["prata_24h"] ** 0.5) / 100, 1) if c > 1 else 0

    return {
        "regiao": regiao,
        "local": local,
        "gerado_em": agora,
        "horas_coleta": round(horas_coleta, 1),
        "movimento_ok": movimento_ok,
        "picos_descartados": descartados,
        "itens_parados": parados,
        "itens": itens,
    }


# --- detalhe de um item -------------------------------------------------
def calcular_item(regiao, item):
    agora = int(time.time())
    d1 = agora - 86400
    con = conectar()
    combos, descartados = historico_limpo(
        con, regiao, agora - 28 * 86400, (ESCALA_HORA, ESCALA_6H_28D),
        ESCALA_6H_28D, "AND item=?", (item,))

    por_cidade = defaultdict(lambda: {"vendas_24h": 0, "prata_24h": 0})
    horas = defaultdict(lambda: [0, 0])
    dias = defaultdict(lambda: [0, 0])
    for (_, loc, _), pontos in combos.items():
        for esc, ts, q, p in pontos:
            if esc == ESCALA_HORA and ts >= d1:
                por_cidade[loc]["vendas_24h"] += q
                por_cidade[loc]["prata_24h"] += p
                horas[ts][0] += q
                horas[ts][1] += p
            elif esc == ESCALA_6H_28D:
                dia = ts // 86400 * 86400
                dias[dia][0] += q
                dias[dia][1] += p

    for r in con.execute("""
        SELECT local,
            MIN(CASE WHEN tipo='offer' THEN preco END) menor_venda,
            MAX(CASE WHEN tipo='request' THEN preco END) maior_compra,
            SUM(CASE WHEN tipo='offer' THEN quantidade ELSE 0 END) estoque
        FROM ordens WHERE regiao=? AND item=? AND ultima_vez>=? AND qualidade<=2
        GROUP BY local""", (regiao, item, agora - JANELA_ORDENS_ATUAIS)):
        por_cidade[r["local"]].update(menor_venda=r["menor_venda"],
                                      maior_compra=r["maior_compra"],
                                      estoque=r["estoque"])
    con.close()

    cidades = []
    for loc, d in por_cidade.items():
        _, nome = ref.local_normalizado(loc)
        cidades.append({"local": loc, "nome": nome,
                        "principal": loc in CIDADES, **d})
    cidades.sort(key=lambda c: (-c["prata_24h"], c["nome"]))
    info = {k: v for k, v in ref.info(item).items() if k != "busca"}
    return {
        "id": item, **info, "cidades": cidades,
        "horas": [{"ts": t, "quantidade": q, "prata": p}
                  for t, (q, p) in sorted(horas.items())],
        "dias": [{"dia": t, "quantidade": q, "prata": p}
                 for t, (q, p) in sorted(dias.items())],
        "picos_descartados": descartados,
    }


# --- comparação entre dois mercados --------------------------------------
TAXA_VENDA = 0.065  # 4% de imposto (premium) + 2,5% de taxa de anúncio
MIN_VENDAS_DIA_COMPARA = 5


def calcular_comparacao(regiao, a, b):
    """Preço médio de 3 dias (qualidade Normal) do mesmo item em dois mercados.

    Os últimos 10 dias servem de referência para descartar picos.
    """
    agora = int(time.time())
    d3 = agora - 3 * 86400
    con = conectar()
    combos, descartados = historico_limpo(
        con, regiao, agora - 10 * 86400, (ESCALA_6H_28D,), ESCALA_6H_28D,
        "AND qualidade=1 AND local IN (?, ?)", (a, b))

    precos = defaultdict(dict)
    for (item, loc, _), pontos in combos.items():
        recentes = [(ts, q, p) for _, ts, q, p in pontos if ts >= d3]
        if not recentes:
            continue
        q = sum(r[1] for r in recentes)
        p = sum(r[2] for r in recentes)
        ultima = max(r[0] for r in recentes) + 6 * 3600
        precos[item][loc] = (q, p / q, ultima)

    ordens = defaultdict(dict)
    for r in con.execute("""
        SELECT item, local,
            MIN(CASE WHEN tipo='offer' THEN preco END) menor_venda,
            MAX(CASE WHEN tipo='request' THEN preco END) maior_compra
        FROM ordens WHERE regiao=? AND qualidade=1 AND ultima_vez>=?
          AND local IN (?, ?)
        GROUP BY item, local""", (regiao, agora - JANELA_ORDENS_ATUAIS, a, b)):
        ordens[r["item"]][r["local"]] = r
    con.close()

    itens = []
    for item, p in precos.items():
        if a not in p or b not in p:
            continue
        (qa, pa, ua), (qb, pb, ub) = p[a], p[b]
        if qa / 3 < MIN_VENDAS_DIA_COMPARA or qb / 3 < MIN_VENDAS_DIA_COMPARA:
            continue
        info = ref.info(item)
        oa, ob = ordens[item].get(a), ordens[item].get(b)
        lucro = pb * (1 - TAXA_VENDA) - pa
        itens.append({
            "id": item, "nome": info["nome"], "tier": info["tier"], "enc": info["enc"],
            "preco_a": round(pa), "preco_b": round(pb),
            "vendas_dia_a": round(qa / 3, 1), "vendas_dia_b": round(qb / 3, 1),
            "diferenca": pct(pb, pa),
            "lucro_unidade": round(lucro),
            "lucro_pct": round(lucro / pa * 100, 1),
            "menor_venda_a": oa["menor_venda"] if oa else None,
            "menor_venda_b": ob["menor_venda"] if ob else None,
            "maior_compra_b": ob["maior_compra"] if ob else None,
            "ultima_venda": min(ua, ub),
        })
    return {"regiao": regiao, "a": a, "b": b, "gerado_em": agora,
            "taxa_venda": TAXA_VENDA, "picos_descartados": descartados,
            "itens": itens}


# --- fabricação: receita, custos e bônus ----------------------------------
URL_FESTIVIDADES = "https://{regiao}.albion-online-data.com/api/v2/stats/festivities"
CACHE_BONUS = 1800


def bonus_do_dia(regiao):
    """Categorias com bônus de produção hoje, vindas do Albion Data Project."""
    chave = ("bonus", regiao)
    agora = time.time()
    if chave in _cache and agora - _cache[chave][0] < CACHE_BONUS:
        return _cache[chave][1]
    try:
        r = httpx.get(URL_FESTIVIDADES.format(regiao=regiao), timeout=15,
                      headers={"Accept-Encoding": "gzip",
                               "User-Agent": "albion-em-alta"})
        dados = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        # Sem resposta: mantém o último valor conhecido, se houver
        return _cache.get(chave, (0, {"bonus": [], "disponivel": False}))[1]

    bonus = []
    for ev in dados.get("Events", []):
        info = receitas.bonus_diario.get(ev.get("UniqueName"))
        if not info:
            continue
        fim = config.ticks_para_epoch(ev["EndTime"])
        if fim < agora:
            continue
        cat = info["cat"]
        bonus.append({
            "cat": cat,
            "nome": receitas_mod.CATEGORIAS.get(cat, cat),
            "valor": info["valor"],
            "termina": fim,
            "cidades": [c["nome"] for c in receitas.cidades
                        if c["mods"].get(cat) and c["local"]],
        })
    valor = {"bonus": bonus, "disponivel": bool(dados),
             "confirmado_em": dados.get("ConfirmedAt")}
    _cache[chave] = (agora, valor)
    return valor


def precos_recentes(con, regiao, ids, qualidade=1, dias=3,
                    janela_ordens=JANELA_ORDENS_ATUAIS):
    """Preço médio dos últimos dias (sem picos) e menor venda atual, por cidade."""
    agora = int(time.time())
    marcas = ",".join("?" * len(ids))
    filtro_q = "AND qualidade=?" if qualidade else ""
    args_q = (qualidade,) if qualidade else ()
    combos, _ = historico_limpo(
        con, regiao, agora - max(10, dias) * 86400, (ESCALA_6H_28D,), ESCALA_6H_28D,
        f"{filtro_q} AND item IN ({marcas})", (*args_q, *ids))
    somas = defaultdict(lambda: [0, 0])
    for (item, loc, _), pontos in combos.items():
        if loc not in CIDADES:
            continue
        for _, ts, q, p in pontos:
            if ts >= agora - dias * 86400:
                somas[(item, loc)][0] += q
                somas[(item, loc)][1] += p
    precos = defaultdict(dict)
    for (item, loc), (q, p) in somas.items():
        precos[item][loc] = {"medio": round(p / q), "vendas_dia": round(q / dias, 1)}
    for r in con.execute(f"""
        SELECT item, local, MIN(preco) menor FROM ordens
        WHERE regiao=? AND tipo='offer' AND qualidade<=? AND ultima_vez>=?
          AND item IN ({marcas}) GROUP BY item, local""",
            (regiao, max(qualidade or 5, 2), agora - janela_ordens, *ids)):
        if r["local"] in CIDADES:
            precos[r["item"]].setdefault(r["local"], {})["menor_venda"] = r["menor"]
    return precos


def _melhor_compra(precos_item):
    """Cidade mais barata para comprar (preço médio; menor venda se faltar)."""
    opcoes = [(p.get("medio") or p.get("menor_venda"), loc)
              for loc, p in precos_item.items()
              if p.get("medio") or p.get("menor_venda")]
    if not opcoes:
        return None, None
    preco, loc = min(opcoes)
    return preco, loc


def _material(rid, qtd, retorna, precos):
    info = ref.info(rid)
    preco, loc = _melhor_compra(precos.get(rid, {}))
    return {
        "id": rid, "nome": info["nome"], "tier": info["tier"], "enc": info["enc"],
        "qtd": qtd, "retorna": retorna,
        "preco": preco, "cidade": CIDADES.get(loc) if loc else None,
        "subtotal": preco * qtd if preco else None,
        "por_cidade": {CIDADES[l]: p for l, p in precos.get(rid, {}).items()},
    }


def calcular_craft(regiao, item):
    rec = receitas.receitas.get(item)
    if not rec:
        return {"fabricavel": False}
    cat = rec["cat"]
    sub = {}  # receitas de refino dos materiais refinados
    for r in rec["recursos"]:
        s = receitas.receitas.get(r["id"])
        if s and s["cat"] in receitas_mod.REFINO:
            sub[r["id"]] = s
    ids = {item} | {r["id"] for r in rec["recursos"]}
    for s in sub.values():
        ids |= {r["id"] for r in s["recursos"]}

    con = conectar()
    precos = precos_recentes(con, regiao, sorted(ids))
    # O item final vende em várias qualidades; usa todas para o preço de venda
    # (itens caros vendem pouco, então a janela é maior: 14 dias e ordens de 48h)
    precos[item] = precos_recentes(con, regiao, [item], qualidade=None, dias=14,
                                   janela_ordens=48 * 3600).get(item, {})
    con.close()

    hoje = {b["cat"]: b["valor"] for b in bonus_do_dia(regiao)["bonus"]}
    materiais = [_material(r["id"], r["qtd"], r["retorna"], precos)
                 for r in rec["recursos"]]
    completo = all(m["preco"] for m in materiais)
    custo_ret = sum(m["subtotal"] or 0 for m in materiais if m["retorna"])
    custo_fixo = sum(m["subtotal"] or 0 for m in materiais if not m["retorna"])
    venda = {loc: p for loc, p in precos.get(item, {}).items()}

    cidades = []
    for c in receitas.cidades:
        b_dia = hoje.get(cat, 0)
        bonus = receitas.bonus(c, cat, b_dia)
        bonus_foco = receitas.bonus(c, cat, b_dia, foco=True)
        rrr, rrr_foco = receitas_mod.taxa_retorno(bonus), receitas_mod.taxa_retorno(bonus_foco)
        pv = venda.get(c["local"], {}) if c["local"] else {}
        cidades.append({
            "nome": c["nome"], "local": c["local"],
            "especialidade": c["mods"].get(cat, 0),
            "bonus_dia": b_dia,
            "bonus": round(bonus, 3), "retorno": round(rrr, 4),
            "bonus_foco": round(bonus_foco, 3), "retorno_foco": round(rrr_foco, 4),
            "custo_materiais": round((custo_ret * (1 - rrr) + custo_fixo) / rec["qtd_fabricada"]) if completo else None,
            "custo_materiais_foco": round((custo_ret * (1 - rrr_foco) + custo_fixo) / rec["qtd_fabricada"]) if completo else None,
            "preco_venda": pv.get("medio") or pv.get("menor_venda"),
        })
    cidades.sort(key=lambda c: -c["bonus"])

    refino = []
    for rid, s in sub.items():
        mats = [_material(r["id"], r["qtd"], r["retorna"], precos) for r in s["recursos"]]
        melhor = max((c for c in receitas.cidades if c["local"]),
                     key=lambda c: receitas.bonus(c, s["cat"], hoje.get(s["cat"], 0)))
        b = receitas.bonus(melhor, s["cat"], hoje.get(s["cat"], 0))
        b_foco = receitas.bonus(melhor, s["cat"], hoje.get(s["cat"], 0), foco=True)
        ok = all(m["preco"] for m in mats)
        bruto = sum(m["subtotal"] for m in mats) if ok else None
        refino.append({
            "id": rid, "nome": ref.info(rid)["nome"], "categoria": s["cat"],
            "nome_categoria": receitas_mod.CATEGORIAS.get(s["cat"], s["cat"]),
            "materiais": mats, "valor": s["valor"],
            "melhor_cidade": melhor["nome"], "bonus": round(b, 3),
            "retorno": round(receitas_mod.taxa_retorno(b), 4),
            "retorno_foco": round(receitas_mod.taxa_retorno(b_foco), 4),
            "custo_refinar": round(bruto * (1 - receitas_mod.taxa_retorno(b))) if ok else None,
            "custo_refinar_foco": round(bruto * (1 - receitas_mod.taxa_retorno(b_foco))) if ok else None,
            "preco_comprar": _melhor_compra(precos.get(rid, {}))[0],
        })

    return {
        "fabricavel": True, "id": item, "categoria": cat,
        "nome_categoria": receitas_mod.CATEGORIAS.get(cat, cat),
        "refino": cat in receitas_mod.REFINO,
        "qtd_fabricada": rec["qtd_fabricada"], "prata_receita": rec["prata"],
        "valor_item": rec["valor"],
        "nutricao": round(rec["valor"] * receitas_mod.FATOR_NUTRICAO, 1),
        "materiais": materiais, "materiais_completos": completo,
        "custo_compra": round((custo_ret + custo_fixo) / rec["qtd_fabricada"]) if completo else None,
        "cidades": cidades, "refino_materiais": refino,
        "bonus_hoje": hoje.get(cat, 0),
    }


# --- ciclos semanais dos insumos -------------------------------------------
ARQ_CICLOS = config.PASTA_DADOS / "ciclos.json"
_ciclos = {"mtime": 0, "dados": None}


def ciclos():
    """Lê dados/ciclos.json (gerado por ciclos.py), recarregando se mudar."""
    if not ARQ_CICLOS.exists():
        return None
    mtime = ARQ_CICLOS.stat().st_mtime
    if mtime != _ciclos["mtime"]:
        _ciclos.update(mtime=mtime, dados=json.loads(ARQ_CICLOS.read_text(encoding="utf-8")))
    return _ciclos["dados"]


CAMPOS_LISTA = ("item", "nome", "tier", "enc", "grupo", "local", "cidade", "vendas_dia",
                "preco_atual", "ema_3d", "posicao", "forca", "amplitude",
                "faixa_compra", "faixa_venda", "margem_liquida", "proxima_compra",
                "faixa_atual", "sinal")


# --- previsão de tendência ------------------------------------------------
CACHE_PREVISAO = 600
MERCADO_NEGRO = 3003
MOTIVOS = {
    "momento_curto": ("preço subiu nas últimas 6h", "preço caiu nas últimas 6h"),
    "momento_24h": ("preço acima da semana", "preço abaixo da semana"),
    "volume": ("procura acima do normal", "vendas fracas, tendem a normalizar"),
    "desvio": ("preço acima do habitual", "preço abaixo do habitual"),
}


def calcular_previsao(regiao):
    if not previsao.ARQ_MODELO.exists():
        return {"disponivel": False}
    modelo = json.loads(previsao.ARQ_MODELO.read_text(encoding="utf-8"))
    con = conectar()
    series = previsao.carregar_series(con, regiao)
    con.close()

    candidatos = []
    for (_, item, loc), (vol, prata, _) in series.items():
        ind = previsao.indicadores(vol, prata, len(vol) - 1)
        if not ind or not previsao.elegivel_top(ind[0], ind[2]):
            continue
        candidatos.append((item, loc, ind))
    if not candidatos:
        return {"disponivel": True, "itens": [], "teste": modelo["teste"]}

    preds = previsao.prever(modelo, [c[2][0] for c in candidatos])
    melhores = {}
    melhor_negro = None
    for (item, loc, ind), pred in zip(candidatos, preds):
        if pred <= 0:
            continue
        if item not in melhores or pred > melhores[item][0]:
            melhores[item] = (float(pred), loc, ind)
        if loc == MERCADO_NEGRO and (melhor_negro is None or pred > melhor_negro[1]):
            melhor_negro = (item, float(pred), ind)

    pesos = np.array(modelo["pesos"])

    def montar(item, pred, loc, ind):
        x, p24, v24 = ind
        z = (np.clip(x, -2, 2) - modelo["media"]) / np.array(modelo["desvio"])
        contrib = z * pesos
        motivos = [MOTIVOS[n][0 if v > 0 else 1]
                   for n, v, c in sorted(zip(modelo["indicadores"], x, contrib),
                                         key=lambda t: -t[2]) if c > 0][:2]
        info = ref.info(item)
        alta = math.exp(pred) - 1
        return {
            "id": item, "nome": info["nome"], "tier": info["tier"], "enc": info["enc"],
            "cidade": CIDADES.get(loc) or ref.local_normalizado(loc)[1],
            "preco_atual": round(p24),
            "preco_previsto": round(p24 * (1 + alta)),
            "alta_prevista": round(alta * 100, 1),
            "vendas_24h": int(v24), "motivos": motivos,
        }

    itens = [montar(item, pred, loc, ind) for item, (pred, loc, ind)
             in sorted(melhores.items(), key=lambda kv: -kv[1][0])[:3]]
    # Destaque do Mercado Negro: maior alta prevista entre as vendas para os NPCs
    negro = montar(melhor_negro[0], melhor_negro[1], MERCADO_NEGRO, melhor_negro[2]) \
        if melhor_negro else None
    return {"disponivel": True, "itens": itens, "mercado_negro": negro,
            "teste": modelo["teste"], "horizonte_h": modelo["horizonte_h"],
            "treinado_em": modelo["treinado_em"], "gerado_em": int(time.time())}


# --- rotas ---------------------------------------------------------------
@app.get("/api/comparar")
def comparar(regiao: str = Query("west"), a: int = Query(...), b: int = Query(...)):
    validar_regiao(regiao)
    if a not in CIDADES or b not in CIDADES or a == b:
        raise HTTPException(400, "Escolha dois mercados diferentes")
    return em_cache(("comparar", regiao, a, b),
                    lambda: calcular_comparacao(regiao, a, b))


@app.get("/api/bonus-do-dia")
def rota_bonus(regiao: str = Query("west")):
    validar_regiao(regiao)
    return bonus_do_dia(regiao)


@app.get("/api/craft/{regiao}/{item}")
def rota_craft(regiao: str, item: str):
    validar_regiao(regiao)
    if item not in ref.itens:
        raise HTTPException(404, "Item desconhecido")
    return em_cache(("craft", regiao, item), lambda: calcular_craft(regiao, item))


@app.get("/api/ciclos")
def rota_ciclos(regiao: str = Query("west")):
    validar_regiao(regiao)
    d = ciclos()
    if not d or regiao not in d["regioes"]:
        return {"disponivel": False}
    r = d["regioes"][regiao]
    return {
        "disponivel": True, "gerado_em": d["gerado_em"], "seg_ref": d["seg_ref"],
        "grupos": r["grupos"], "resumo": r["resumo"],
        "series": [{k: x[k] for k in CAMPOS_LISTA} for x in r["series"]],
    }


@app.get("/api/ciclo/{regiao}/{item}")
def rota_ciclo_item(regiao: str, item: str):
    validar_regiao(regiao)
    d = ciclos()
    if not d or regiao not in d["regioes"]:
        return {"disponivel": False, "cidades": []}
    cidades = [x for x in d["regioes"][regiao]["series"] if x["item"] == item]
    cidades.sort(key=lambda x: -x["vendas_dia"])
    return {"disponivel": True, "seg_ref": d["seg_ref"], "cidades": cidades}


@app.get("/api/previsao")
def rota_previsao(regiao: str = Query("west")):
    validar_regiao(regiao)
    chave = ("previsao", regiao)
    agora = time.time()
    if chave in _cache and agora - _cache[chave][0] < CACHE_PREVISAO:
        return _cache[chave][1]
    valor = calcular_previsao(regiao)
    _cache[chave] = (agora, valor)
    return valor



@app.get("/api/regioes")
def regioes():
    con = conectar()
    status = {r["regiao"]: dict(r) for r in con.execute("SELECT * FROM status")}
    con.close()
    return [{"id": k, "nome": v["nome"], "status": status.get(k)}
            for k, v in REGIOES.items()]


@app.get("/api/cidades")
def cidades():
    return [{"id": k, "nome": v} for k, v in CIDADES.items()]


@app.get("/api/em-alta")
def em_alta(regiao: str = Query("west"), cidade: int | None = Query(None)):
    validar_regiao(regiao)
    if cidade is not None and cidade not in CIDADES:
        raise HTTPException(404, "Cidade inválida")
    return em_cache(("ranking", regiao, cidade),
                    lambda: calcular_ranking(regiao, cidade))


@app.get("/api/busca")
def busca(q: str = Query(..., min_length=2, max_length=60), limite: int = 30):
    """Procura no catálogo inteiro, inclusive itens sem vendas recentes."""
    termos = sem_acento(q).split()
    achados = [
        {"id": codigo, "nome": i["nome"], "tier": i["tier"], "enc": i["enc"]}
        for codigo, i in ref.itens.items()
        if all(t in i["busca"] for t in termos)
    ]
    achados.sort(key=lambda i: (len(i["nome"]), i["tier"], i["enc"]))
    return achados[:min(limite, 100)]


@app.get("/api/item/{regiao}/{item}")
def item(regiao: str, item: str):
    validar_regiao(regiao)
    if item not in ref.itens:
        raise HTTPException(404, "Item desconhecido")
    return em_cache(("item", regiao, item), lambda: calcular_item(regiao, item))


@app.get("/")
def pagina():
    return FileResponse(RAIZ / "site" / "index.html")


app.mount("/static", StaticFiles(directory=RAIZ / "site"), name="static")


@app.on_event("startup")
def aquecer_previsao():
    """Calcula a previsão em segundo plano para o primeiro visitante não esperar."""
    import threading

    def aquecer():
        for r in REGIOES:
            try:
                rota_previsao(r)
            except Exception as e:  # banco vazio, modelo ausente etc.
                print(f"Previsão de {r} indisponível: {e}")

    threading.Thread(target=aquecer, daemon=True).start()
