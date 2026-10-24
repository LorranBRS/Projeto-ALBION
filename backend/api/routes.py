"""Rotas /api/v1 do módulo preditivo (Task 4).

    GET /api/v1/predictions/{item_id}   previsão 12h/24h, intervalos, z-score e sinal
    GET /api/v1/signals                 sinal de cada item (para os selos da interface)
    GET /api/v1/crafting/opportunities  fabricações com margem > 15%, ordenadas pelo insumo mais barato (z-score)
    GET /api/v1/model                   métricas de validação do modelo
"""
import sqlite3
import threading
import time

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from backend.analytics import features as F
from backend.services.signals import generate_trade_signals
from config import BANCO, REGIOES
from itens import CIDADES

router = APIRouter(prefix="/api/v1")

CACHE_LOTE = 1800            # recalcula os sinais de todos os itens a cada 30 min
MARGEM_MINIMA = 1.15
DIAS_HISTORICO_GRAFICO = 14
MAX_IDADE_VENDA = 3 * 86400  # preço sem venda há mais de 3 dias não conta
JANELA_ORDENS_H = 24         # ordens só aparecem quando alguém abre o mercado no jogo
_lote, _trava = {}, threading.Lock()
_preditor = None


def preditor():
    global _preditor
    if _preditor is None:
        from backend.models.predictor import ARQ_META, Preditor
        if not ARQ_META.exists():
            raise HTTPException(503, "Modelo ainda não treinado (rode: python -m backend.models.predictor)")
        _preditor = Preditor()
    return _preditor


def _servidor():
    import servidor  # importação tardia: servidor.py inclui este router
    return servidor


def _num(v, casas=None):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return round(float(v), casas) if casas is not None else round(float(v))


def _resumo_linha(linha, prev, i):
    """Junta features + previsão de uma linha num dicionário com o sinal."""
    p12, p24 = prev["12h"], prev["24h"]
    dados = {
        "preco_atual": float(linha["price"]),
        "price_pred_12h": float(p12["price_pred"][i]),
        "price_pred_lower": float(p12["price_pred_lower"][i]),
        "price_pred_upper": float(p12["price_pred_upper"][i]),
        "z_score": None if np.isnan(linha["z_score_14d"]) else float(linha["z_score_14d"]),
    }
    sinal = generate_trade_signals(dados)
    return {
        "local": int(linha["local"]), "cidade": CIDADES.get(int(linha["local"])),
        "preco_atual": _num(linha["price"]),
        "sell_price_min": _num(linha.get("sell_price_min")),
        "buy_price_max": _num(linha.get("buy_price_max")),
        "spread_relative": _num(linha.get("spread_relative"), 4),
        "z_score_14d": _num(linha["z_score_14d"], 2),
        "sma_14d": _num(linha["sma_14d"]),
        "volume_rolling_7d": _num(linha["volume_rolling_7d"], 1),
        "price_pred_12h": _num(p12["price_pred"][i]),
        "price_pred_lower": _num(p12["price_pred_lower"][i]),
        "price_pred_upper": _num(p12["price_pred_upper"][i]),
        "price_pred_lower_95": _num(p12["price_pred_lower_95"][i]),
        "price_pred_upper_95": _num(p12["price_pred_upper_95"][i]),
        "price_pred_24h": _num(p24["price_pred"][i]),
        "price_pred_24h_lower": _num(p24["price_pred_lower"][i]),
        "price_pred_24h_upper": _num(p24["price_pred_upper"][i]),
        "price_pred_24h_lower_95": _num(p24["price_pred_lower_95"][i]),
        "price_pred_24h_upper_95": _num(p24["price_pred_upper_95"][i]),
        "ts": int(linha["ts"]),
        **sinal,
        **_horizontes_longos(linha, prev, i),
    }


Z80, Z95 = 1.2816, 1.96
SEMANAS_PROJECAO = 4


def _horizontes_longos(linha, prev, i):
    """Previsões de 3 e 7 dias (modelo) e projeção de 4 semanas (estatística).

    A projeção parte da previsão de 7 dias e soma a incerteza das semanas
    seguintes pela volatilidade semanal real da série (passeio aleatório):
    desvio_k = sqrt(desvio_semana1² + vol_semanal² × (k − 1)).
    Não é validada: o histórico disponível (~30 dias) não permite testar 4 semanas.
    """
    if "7d" not in prev:
        return {}
    ts = int(linha["ts"])
    saida = {"horizontes": []}
    for nome, horas in (("12h", 12), ("24h", 24), ("3d", 72), ("7d", 168)):
        if nome not in prev:
            continue
        p = prev[nome]
        saida["horizontes"].append({
            "nome": nome, "ts": ts + horas * 3600,
            "pred": _num(p["price_pred"][i]),
            "lo80": _num(p["price_pred_lower"][i]), "hi80": _num(p["price_pred_upper"][i]),
            "lo95": _num(p["price_pred_lower_95"][i]), "hi95": _num(p["price_pred_upper_95"][i]),
        })
    p7 = prev["7d"]
    centro = float(p7["price_pred"][i])
    lo, hi = float(p7["price_pred_lower"][i]), float(p7["price_pred_upper"][i])
    s1 = max(np.log(hi / lo) / (2 * Z80), 1e-4) if lo > 0 else 0.1
    vol = linha.get("vol_semanal")
    vol = float(vol) if vol is not None and not np.isnan(vol) else s1
    semanas = []
    for k in range(1, SEMANAS_PROJECAO + 1):
        dp = np.sqrt(s1 ** 2 + vol ** 2 * (k - 1))
        semanas.append({
            "semana": k, "ts": ts + k * 7 * 86400, "pred": _num(centro),
            "lo80": _num(centro * np.exp(-Z80 * dp)), "hi80": _num(centro * np.exp(Z80 * dp)),
            "lo95": _num(centro * np.exp(-Z95 * dp)), "hi95": _num(centro * np.exp(Z95 * dp)),
        })
    preco = float(linha["price"])
    saida.update(
        semanas=semanas,
        price_pred_3d=_num(prev["3d"]["price_pred"][i]) if "3d" in prev else None,
        price_pred_7d=_num(centro), price_pred_7d_lower=_num(lo), price_pred_7d_upper=_num(hi),
        var_7d=round(centro / preco - 1, 4) if preco else None,
        incerteza_7d=round((hi - lo) / centro, 4) if centro else None,
        vol_semanal=_num(vol, 4),
    )
    return saida


def _ultimas_linhas(df):
    """Última linha de cada (item, local), com a data da última venda real."""
    ultima_venda = df[df["has_sale"]].groupby(["item", "local"])["ts"].max().rename("ultima_venda")
    ult = df.groupby(["item", "local"], sort=False).tail(1).merge(
        ultima_venda, on=["item", "local"], how="left")
    return ult[ult["local"].isin(CIDADES.keys())].reset_index(drop=True)


def calcular_lote(regiao):
    """Features + previsão para a última posição de todas as séries da região."""
    srv = _servidor()
    con = sqlite3.connect(BANCO)
    df = F.carregar_historico(regiao, con=con)
    ordens = F.ordens_atuais(regiao, con=con, janela_h=JANELA_ORDENS_H)
    con.close()
    df = F.build_market_features(df, info_itens=srv.ref.info, ordens=ordens)
    ult = _ultimas_linhas(df)
    dias_historico = round(float(df["ts"].max() - df["ts"].min()) / 86400, 1) if len(df) else 0
    del df
    agora = time.time()
    ult = ult[(ult["volume_rolling_7d"] >= 1) & (ult["ultima_venda"] >= agora - MAX_IDADE_VENDA)]
    ult = ult.reset_index(drop=True)
    prev = preditor().prever(ult) if len(ult) else None
    linhas = [_resumo_linha(ult.iloc[i], prev, i) | {"item": ult.at[i, "item"]}
              for i in range(len(ult))]
    return {"gerado_em": int(agora), "linhas": linhas, "dias_historico": dias_historico}


_travas_regiao = {r: threading.Lock() for r in REGIOES}


def lote(regiao):
    """Lote em cache; só um cálculo por região de cada vez (os demais esperam por ele)."""
    with _travas_regiao[regiao]:
        with _trava:
            atual = _lote.get(regiao)
        if atual and time.time() - atual["gerado_em"] < CACHE_LOTE:
            return atual
        novo = calcular_lote(regiao)
        with _trava:
            _lote[regiao] = novo
        return novo


def aquecer():
    """Calcula os lotes em segundo plano ao iniciar o servidor."""
    def rodar():
        for r in REGIOES:
            try:
                lote(r)
            except Exception as e:  # modelo ausente, banco vazio etc.
                print(f"Sinais de {r} indisponíveis: {e}")
    threading.Thread(target=rodar, daemon=True).start()


# --- rotas --------------------------------------------------------------------
@router.get("/model")
def modelo():
    m = preditor().meta
    return {"treinado_em": m["treinado_em"], "regiao_treino": m["regiao_treino"],
            "horizontes": {k: {"dobras": v["dobras"], "sinais_teste": v["sinais_teste"]}
                           for k, v in m["horizontes"].items()}}


@router.get("/signals")
def sinais(regiao: str = Query("west")):
    if regiao not in REGIOES:
        raise HTTPException(404, "Região inválida")
    lt = lote(regiao)
    por_item = {}
    for l in lt["linhas"]:
        por_item.setdefault(l["item"], []).append(
            {k: l[k] for k in ("local", "cidade", "sinal", "z_score_14d", "preco_atual",
                               "price_pred_12h", "volume_rolling_7d")})
    for v in por_item.values():
        v.sort(key=lambda x: -(x["volume_rolling_7d"] or 0))
    return {"gerado_em": lt["gerado_em"], "itens": por_item}


@router.get("/forecast")
def previsao_longa(regiao: str = Query("west")):
    """Previsão de 7 dias de todos os itens líquidos (uma cidade por item: a mais negociada)."""
    if regiao not in REGIOES:
        raise HTTPException(404, "Região inválida")
    srv = _servidor()
    lt = lote(regiao)
    meta = preditor().meta["horizontes"]
    if "7d" not in meta:
        raise HTTPException(503, "Modelo de 7 dias ainda não treinado")
    por_item = {}
    for l in lt["linhas"]:
        if l.get("var_7d") is None or (l["volume_rolling_7d"] or 0) < 5:
            continue
        por_item.setdefault(l["item"], []).append(l)
    itens = []
    for item, linhas in por_item.items():
        info = srv.ref.info(item)
        cidades = sorted(linhas, key=lambda l: -(l["volume_rolling_7d"] or 0))
        itens.append({
            "id": item, "nome": info["nome"], "tier": info["tier"], "enc": info["enc"],
            "cidades": [{k: l[k] for k in ("local", "cidade", "preco_atual", "price_pred_3d",
                                           "price_pred_7d", "price_pred_7d_lower", "price_pred_7d_upper",
                                           "var_7d", "incerteza_7d", "volume_rolling_7d", "sinal")}
                        | {"semana4": l["semanas"][-1]} for l in cidades],
        })
    return {
        "gerado_em": lt["gerado_em"], "itens": itens,
        "validacao": {h: {"dobras": meta[h]["dobras"]} for h in ("3d", "7d") if h in meta},
        "dias_historico": lt.get("dias_historico", 0),
    }


@router.get("/predictions/{item_id}")
def previsao_item(item_id: str, regiao: str = Query("west"), cidade: int | None = Query(None)):
    srv = _servidor()
    if regiao not in REGIOES:
        raise HTTPException(404, "Região inválida")
    if item_id not in srv.ref.itens:
        raise HTTPException(404, "Item desconhecido")
    con = sqlite3.connect(BANCO)
    df = F.carregar_historico(regiao, itens={item_id}, con=con)
    ordens = F.ordens_atuais(regiao, con=con, itens={item_id}, janela_h=JANELA_ORDENS_H)
    con.close()
    if df.empty:
        return {"disponivel": False, "motivo": "sem histórico de vendas (qualidade Normal) para este item"}
    df = F.build_market_features(df, info_itens=srv.ref.info, ordens=ordens)
    ult = _ultimas_linhas(df)
    if cidade is not None:
        ult = ult[ult["local"] == cidade]
    ult = ult.dropna(subset=["volume_rolling_7d"]).sort_values("volume_rolling_7d", ascending=False)
    if ult.empty:
        return {"disponivel": False, "motivo": "sem dados suficientes nesta cidade"}
    ult = ult.reset_index(drop=True)
    prev = preditor().prever(ult)
    cidades = [_resumo_linha(ult.iloc[i], prev, i) | {"ultima_venda": int(ult.at[i, "ultima_venda"])}
               for i in range(len(ult))]
    principal = cidades[0]
    desde = df["ts"].max() - DIAS_HISTORICO_GRAFICO * 86400
    hist = df[(df["local"] == principal["local"]) & (df["ts"] >= desde) & df["has_sale"]]
    info = srv.ref.info(item_id)
    meta = preditor().meta["horizontes"]["12h"]
    return {
        "disponivel": True, "item": item_id, "nome": info["nome"],
        "tier": info["tier"], "enc": info["enc"],
        **principal, "cidades": cidades,
        "historico": [{"ts": int(t), "preco": round(float(p))}
                      for t, p in zip(hist["ts"], hist["price"])],
        "validacao": {"dobras": meta["dobras"], "sinais_teste": meta["sinais_teste"]},
        "validacao_longa": {h: preditor().meta["horizontes"][h]["dobras"]
                            for h in ("3d", "7d") if h in preditor().meta["horizontes"]},
        "historico_longo": [{"ts": int(t), "preco": round(float(p))} for t, p in zip(
            *df[(df["local"] == principal["local"]) & df["has_sale"]][["ts", "price"]].to_numpy().T)],
    }


@router.get("/crafting/opportunities")
def oportunidades(regiao: str = Query("west"), limite: int = Query(20, ge=1, le=100)):
    if regiao not in REGIOES:
        raise HTTPException(404, "Região inválida")
    lt, itens = calcular_oportunidades(regiao)
    return {"gerado_em": lt["gerado_em"], "margem_minima": MARGEM_MINIMA,
            "total": len(itens), "itens": itens[:limite]}


def calcular_oportunidades(regiao, margem_minima=MARGEM_MINIMA):
    srv = _servidor()
    lt = lote(regiao)
    # Preço e z-score mais recentes de cada item por cidade
    por_item = {}
    for l in lt["linhas"]:
        por_item.setdefault(l["item"], []).append(l)
    hoje = {b["cat"]: b["valor"] for b in srv.bonus_do_dia(regiao)["bonus"]}

    itens = []
    for item, rec in srv.receitas.receitas.items():
        # Sem categoria = transmutação (ex.: minério T5 + prata -> T6), não é fabricação
        if not rec["cat"]:
            continue
        venda = [l for l in por_item.get(item, []) if (l["volume_rolling_7d"] or 0) >= 1]
        if not venda or not rec["valor"]:
            continue
        melhor_venda = max(venda, key=lambda l: l["preco_atual"])
        materiais, precos = [], {}
        for r in rec["recursos"]:
            # Só cidades com liquidez: evita preço de uma venda isolada
            opcoes = [l for l in por_item.get(r["id"], []) if (l["volume_rolling_7d"] or 0) >= 5]
            if not opcoes:
                break
            m = min(opcoes, key=lambda l: l["preco_atual"])
            precos[r["id"]] = m["preco_atual"]
            materiais.append({"id": r["id"], "nome": srv.ref.info(r["id"])["nome"], "qtd": r["qtd"],
                              "preco": m["preco_atual"], "cidade": m["cidade"],
                              "z_score_14d": m["z_score_14d"], "retorna": r["retorna"]})
        else:
            # Melhor cidade para fabricar (maior bônus da categoria, sem foco)
            locais = [c for c in srv.receitas.cidades if c["local"]]
            oficina = max(locais, key=lambda c: srv.receitas.bonus(c, rec["cat"], hoje.get(rec["cat"], 0)))
            bonus = srv.receitas.bonus(oficina, rec["cat"], hoje.get(rec["cat"], 0))
            rrf = 1 - 1 / (1 + bonus)
            custo = F.custo_teorico(rec, precos, rrf)
            if not custo:
                continue
            razao = melhor_venda["preco_atual"] / custo
            zs = [m["z_score_14d"] for m in materiais if m["z_score_14d"] is not None]
            if razao <= margem_minima or not zs:
                continue
            info = srv.ref.info(item)
            itens.append({
                "id": item, "nome": info["nome"], "tier": info["tier"], "enc": info["enc"],
                "categoria": rec["cat"], "sell_price": melhor_venda["preco_atual"],
                "sell_city": melhor_venda["cidade"],
                "vendas_dia": melhor_venda["volume_rolling_7d"],
                "cost_theoretical": round(custo), "crafting_margin_ratio": round(razao, 3),
                "rrf": round(rrf, 4), "craft_city": oficina["nome"],
                "insumo_z_min": min(zs), "materiais": materiais,
                "nutricao": round(rec["valor"] * 0.1125 / rec["qtd_fabricada"], 2),
                "sinal_item": melhor_venda["sinal"],
            })
    itens.sort(key=lambda x: x["insumo_z_min"])
    return lt, itens


# --- calculadora pessoal ---------------------------------------------------------
TAXA_PREMIUM, TAXA_SEM_PREMIUM = 0.065, 0.105   # (4% ou 8% de imposto) + 2,5% de anúncio
FATIA_LIQUIDEZ = 0.2                             # vender no máx. 20% do volume diário do item
ROI_MAXIMO = 3.0                                 # acima disso é quase sempre erro de dado


def _preco_compra(l):
    """Conservador: o maior entre o preço atual e a média de 14 dias (ignora quedas isoladas)."""
    return max(v for v in (l["preco_atual"], l.get("sma_14d")) if v)


def _preco_venda(l):
    """Conservador: o menor entre o preço atual e a média de 14 dias (ignora picos isolados)."""
    return min(v for v in (l["preco_atual"], l.get("sma_14d")) if v)


def _plano(tipo, item, nome, tier, enc, compra, venda, preco_compra, receita_unit,
           vendas_dia, prata, max_fatia, prazo, confianca, detalhe, pessimista_unit=None):
    """Quantidade limitada pela prata, pela fatia máxima por item e pela liquidez."""
    if preco_compra <= 0 or receita_unit <= preco_compra:
        return None
    if receita_unit / preco_compra - 1 > ROI_MAXIMO:
        return None
    limite_prata = prata * max_fatia
    limite_liquidez = max(1, int((vendas_dia or 0) * FATIA_LIQUIDEZ))
    qtd = int(min(limite_prata // preco_compra, limite_liquidez))
    if qtd < 1:
        return None
    lucro_unit = receita_unit - preco_compra
    return {
        "tipo": tipo, "item": item, "nome": nome, "tier": tier, "enc": enc,
        "compra": compra, "venda": venda, "qtd": qtd,
        "preco_compra": round(preco_compra), "receita_unit": round(receita_unit),
        "investimento": round(qtd * preco_compra), "lucro": round(qtd * lucro_unit),
        "lucro_pessimista": round(qtd * (pessimista_unit - preco_compra)) if pessimista_unit else None,
        "roi": round(lucro_unit / preco_compra, 4), "prazo": prazo, "confianca": confianca,
        "limitado_por": "liquidez" if qtd == limite_liquidez and limite_liquidez < limite_prata // preco_compra else "prata",
        "detalhe": detalhe,
    }


@router.get("/calculadora")
def calculadora(regiao: str = Query("west"),
                prata: float = Query(..., gt=0, description="prata disponível"),
                premium: bool = Query(True),
                taxa_estacao: float = Query(500, ge=0, description="prata por 100 de nutrição"),
                max_fatia: float = Query(0.25, gt=0, le=1, description="fração máxima da prata por item"),
                estrategias: str = Query("revenda,fabricar,segurar"),
                comprar_em: int | None = Query(None, description="só comprar nesta cidade"),
                tier_min: int = Query(0, ge=0, le=8)):
    """O que dá para fazer com a prata informada: planos ordenados por lucro e carteira sugerida."""
    if regiao not in REGIOES:
        raise HTTPException(404, "Região inválida")
    srv = _servidor()
    taxa = TAXA_PREMIUM if premium else TAXA_SEM_PREMIUM
    tipos = set(estrategias.split(","))
    lt = lote(regiao)
    por_item = {}
    for l in lt["linhas"]:
        if l["preco_atual"] and (l["volume_rolling_7d"] or 0) >= 5:
            por_item.setdefault(l["item"], []).append(l)
    planos = []

    for item, linhas in por_item.items():
        info = srv.ref.info(item)
        if info["tier"] < tier_min:
            continue
        # 1) Revenda: comprar na cidade mais barata e vender na mais cara (Mercado Negro só compra)
        if "revenda" in tipos and len(linhas) >= 2:
            origens = [l for l in linhas if l["local"] != 3003
                       and (comprar_em is None or l["local"] == comprar_em)]
            if origens:
                o = min(origens, key=_preco_compra)
                destinos = [l for l in linhas if l["local"] != o["local"]]
                if destinos:
                    d = max(destinos, key=_preco_venda)
                    pc, pv = _preco_compra(o), _preco_venda(d)
                    p = _plano("revenda", item, info["nome"], info["tier"], info["enc"],
                               o["cidade"], d["cidade"], pc, pv * (1 - taxa),
                               d["volume_rolling_7d"], prata, max_fatia, "transporte (horas)", "média",
                               f"Comprar em {o['cidade']} por ~{round(pc)}, vender em {d['cidade']} "
                               f"por ~{round(pv)} (menos {taxa:.1%} de taxas)")
                    if p:
                        planos.append(p)
        # 3) Segurar 7 dias: comprar agora e vender depois, se o modelo prevê alta acima das taxas
        if "segurar" in tipos:
            for l in linhas:
                if l["local"] == 3003 or (comprar_em is not None and l["local"] != comprar_em):
                    continue
                if l.get("price_pred_7d") is None or (l.get("incerteza_7d") or 1) > 0.4:
                    continue
                pc = max(l["preco_atual"], l.get("sell_price_min") or 0)
                p = _plano("segurar", item, info["nome"], info["tier"], info["enc"],
                           l["cidade"], l["cidade"], pc, l["price_pred_7d"] * (1 - taxa),
                           l["volume_rolling_7d"], prata, max_fatia, "7 dias", "baixa",
                           f"Previsão de 7 dias: {l['price_pred_7d']} (faixa de 80%: "
                           f"{l['price_pred_7d_lower']} a {l['price_pred_7d_upper']})",
                           pessimista_unit=l["price_pred_7d_lower"] * (1 - taxa))
                if p:
                    planos.append(p)

    # 2) Fabricar: custo teórico + taxa da estação
    if "fabricar" in tipos:
        _, oport = calcular_oportunidades(regiao, margem_minima=1.0)
        for x in oport:
            if x["tier"] < tier_min:
                continue
            if comprar_em is not None and any(m["cidade"] != CIDADES.get(comprar_em) for m in x["materiais"]):
                continue
            custo = x["cost_theoretical"] + x["nutricao"] * taxa_estacao / 100
            p = _plano("fabricar", x["id"], x["nome"], x["tier"], x["enc"],
                       ", ".join(sorted({m["cidade"] for m in x["materiais"]})), x["sell_city"],
                       custo, x["sell_price"] * (1 - taxa), x["vendas_dia"], prata, max_fatia,
                       "fabricação + venda", "média",
                       f"Fabricar em {x['craft_city']} (retorno {x['rrf']:.1%}): "
                       + " + ".join(f"{m['qtd']}× {m['nome']}" for m in x["materiais"]))
            if p:
                planos.append(p)

    planos.sort(key=lambda p: -p["lucro"])

    # Carteira: maior lucro (já limitado por prata e liquidez) primeiro, um plano por item
    carteira, restante, usados = [], prata, set()
    for p in planos:
        if p["item"] in usados or restante < p["preco_compra"]:
            continue
        qtd = int(min(p["qtd"], restante // p["preco_compra"]))
        if qtd < 1:
            continue
        fator = qtd / p["qtd"]
        # Não vale ocupar espaço com um plano de lucro desprezível só para gastar o troco
        if p["lucro"] * fator < max(1000, prata * 0.001):
            continue
        carteira.append(p | {"qtd": qtd, "investimento": round(qtd * p["preco_compra"]),
                             "lucro": round(p["lucro"] * fator),
                             "lucro_pessimista": round(p["lucro_pessimista"] * fator)
                             if p["lucro_pessimista"] is not None else None})
        restante -= qtd * p["preco_compra"]
        usados.add(p["item"])
        if len(carteira) >= 12:
            break

    return {
        "gerado_em": lt["gerado_em"], "prata": prata, "taxa_venda": taxa,
        "planos": planos[:30], "total_planos": len(planos),
        "carteira": carteira,
        "carteira_resumo": {
            "investido": round(prata - restante), "sobra": round(restante),
            "lucro": sum(p["lucro"] for p in carteira),
        },
    }
