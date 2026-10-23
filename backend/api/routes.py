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
    }


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
    del df
    agora = time.time()
    ult = ult[(ult["volume_rolling_7d"] >= 1) & (ult["ultima_venda"] >= agora - MAX_IDADE_VENDA)]
    ult = ult.reset_index(drop=True)
    prev = preditor().prever(ult) if len(ult) else None
    linhas = [_resumo_linha(ult.iloc[i], prev, i) | {"item": ult.at[i, "item"]}
              for i in range(len(ult))]
    return {"gerado_em": int(agora), "linhas": linhas}


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
    }


@router.get("/crafting/opportunities")
def oportunidades(regiao: str = Query("west"), limite: int = Query(20, ge=1, le=100)):
    srv = _servidor()
    if regiao not in REGIOES:
        raise HTTPException(404, "Região inválida")
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
            if razao <= MARGEM_MINIMA or not zs:
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
                "sinal_item": melhor_venda["sinal"],
            })
    itens.sort(key=lambda x: x["insumo_z_min"])
    return {"gerado_em": lt["gerado_em"], "margem_minima": MARGEM_MINIMA,
            "total": len(itens), "itens": itens[:limite]}
