"""Ciclos semanais de preço dos insumos (recursos, refinados, comida, poções,
fazenda/ervas, montarias) e previsão para os próximos 7 dias.

Métodos (por item e cidade, blocos de 6h dos últimos 28 dias, sem picos):
    STL (period=7 dias, robusta)  -> curva semanal: % acima/abaixo do normal
                                     em cada dia/faixa de 6h; força do ciclo
    EMA de 3 dias                 -> nível atual do preço
    Holt-Winters (suavização exponencial com tendência amortecida e
    sazonalidade semanal)         -> previsão dos próximos 7 dias

Validação fora da amostra (última semana separada):
    - erro da previsão Holt-Winters x "repetir a semana anterior" x "último preço"
    - estratégia: comprar no fundo e vender no pico apontados pelas 3 semanas
      anteriores, medindo o resultado real na última semana

Uso:
    python ciclos.py              # gera dados/ciclos.json (rodar 1x por dia)
"""
import os

# Uma thread de álgebra linear por processo: o paralelismo vem do Pool
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import json  # noqa: E402
import sqlite3  # noqa: E402
import time
import warnings
from multiprocessing import Pool

import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.seasonal import STL

import previsao
from config import BANCO, PASTA_DADOS, REGIOES
from itens import CIDADES, Referencia
from receitas import PASTA_BRUTO

ARQ_CICLOS = PASTA_DADOS / "ciclos.json"
BLOCO = 6 * 3600
SEMANA = 28                 # blocos de 6h numa semana
MIN_BLOCOS = 3 * SEMANA     # pelo menos 3 semanas de série
MIN_COBERTURA = 0.7         # fração dos blocos com venda
MIN_VENDAS_DIA = 20
TAXA_VENDA = 0.065
SEG_REF = 4 * 86400         # 1970-01-05 (segunda) 00:00 UTC, referência da semana

# Grupos de insumos: (shopcategory, shopsubcategory1) do items.json bruto
GRUPOS = {
    ("crafting", "resources"): "Recursos brutos",
    ("crafting", "refinedresources"): "Refinados",
    ("consumables", "food"): "Comida",
    ("consumables", "potions"): "Poções",
    ("crafting", "fish"): "Peixes",
    ("crafting", "alchemy"): "Fazenda e ervas",
    ("farming", "farm"): "Fazenda e ervas",
    ("farming", "farmingproducts"): "Fazenda e ervas",
    ("farming", "herbgarden"): "Fazenda e ervas",
    ("farming", "kennel"): "Fazenda e ervas",
    ("farming", "pasture"): "Fazenda e ervas",
    ("mounts", "basemounts"): "Montarias",
    ("mounts", "battle_mount"): "Montarias",
    ("mounts", "raremounts"): "Montarias",
}


def grupos_dos_itens():
    itens = json.loads((PASTA_BRUTO / "items.json").read_text(encoding="utf-8"))["items"]
    grupos = {}
    for g in itens.values():
        for x in (g if isinstance(g, list) else [g]):
            if not isinstance(x, dict) or "@uniquename" not in x:
                continue
            grupo = GRUPOS.get((x.get("@shopcategory"), x.get("@shopsubcategory1")))
            if grupo:
                nivel = int(x.get("@enchantmentlevel") or 0)
                nome = x["@uniquename"]
                grupos[f"{nome}@{nivel}" if nivel else nome] = grupo
    return grupos


def faixa_semana(ts):
    """Índice 0..27 do bloco de 6h na semana (segunda 00h UTC = 0)."""
    return int(((ts - SEG_REF) // BLOCO) % SEMANA)


def serie_preco(vol, prata):
    """Preço por bloco com lacunas preenchidas por interpolação linear."""
    p = np.where(vol > 0, prata / np.maximum(vol, 1), np.nan)
    idx = np.arange(len(p))
    ok = ~np.isnan(p)
    return np.interp(idx, idx[ok], p[ok]), ok.mean()


def ema(x, dias=3):
    alfa = 2 / (dias * 4 + 1)
    nivel = x[0]
    for v in x[1:]:
        nivel = alfa * v + (1 - alfa) * nivel
    return nivel


def perfil_semanal(log_p, t0):
    """Curva semanal (fração acima/abaixo do normal por faixa) e força do ciclo."""
    stl = STL(log_p, period=SEMANA, robust=True).fit()
    faixas = np.array([faixa_semana(t0 + i * BLOCO) for i in range(len(log_p))])
    sazonal = np.array([stl.seasonal[faixas == k].mean() for k in range(SEMANA)])
    sazonal -= sazonal.mean()
    # Força sazonal (Hyndman): 1 - Var(resíduo) / Var(sazonal + resíduo)
    forca = max(0.0, 1 - np.var(stl.resid) / np.var(stl.seasonal + stl.resid))
    return np.exp(sazonal) - 1, forca


def holt_winters(log_p, passos):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo = ExponentialSmoothing(
            log_p, trend="add", damped_trend=True, seasonal="add",
            seasonal_periods=SEMANA, initialization_method="estimated").fit()
    return np.exp(modelo.forecast(passos))


def mape(real, prev):
    return float(np.mean(np.abs(prev - real) / real))


def compra_venda(perfil):
    """Melhor faixa para comprar (fundo) e vender depois dela (pico) na semana."""
    k_compra = int(np.argmin(perfil))
    # O pico de venda precisa vir depois da compra (dentro dos 7 dias seguintes)
    depois = [(k_compra + d) % SEMANA for d in range(1, SEMANA)]
    k_venda = max(depois, key=lambda k: perfil[k])
    margem = (1 + perfil[k_venda]) / (1 + perfil[k_compra]) * (1 - TAXA_VENDA) - 1
    return k_compra, k_venda, margem


def proxima(k, depois_de):
    """Timestamp da próxima ocorrência da faixa k após o instante dado."""
    inicio = (depois_de - SEG_REF) // BLOCO * BLOCO + SEG_REF
    for i in range(1, SEMANA + 1):
        ts = inicio + i * BLOCO
        if faixa_semana(ts) == k:
            return int(ts)
    return None


def testar_estrategia(preco, log_p, t0):
    """Usa 3 semanas para achar fundo/pico e mede o resultado na semana seguinte."""
    corte = len(preco) - SEMANA
    if corte < 2 * SEMANA + 4:
        return None
    perfil, _ = perfil_semanal(log_p[:corte], t0)
    k_c, k_v, _ = compra_venda(perfil)
    tempos = t0 + np.arange(len(preco)) * BLOCO
    i_c = next((i for i in range(corte, len(preco)) if faixa_semana(tempos[i]) == k_c), None)
    if i_c is None:
        return None
    i_v = next((i for i in range(i_c + 1, len(preco)) if faixa_semana(tempos[i]) == k_v), None)
    if i_v is None:
        return None
    return float(preco[i_v] / preco[i_c] * (1 - TAXA_VENDA) - 1)


def analisar_serie(vol, prata, t0):
    preco, cobertura = serie_preco(vol, prata)
    if len(preco) < MIN_BLOCOS or cobertura < MIN_COBERTURA:
        return None
    vendas_dia = vol[-SEMANA:].sum() / 7
    if vendas_dia < MIN_VENDAS_DIA:
        return None
    log_p = np.log(preco)
    perfil, forca = perfil_semanal(log_p, t0)
    k_c, k_v, margem = compra_venda(perfil)

    # Previsão: valida na última semana e depois prevê a próxima com tudo
    real = preco[-SEMANA:]
    prev_teste = holt_winters(log_p[:-SEMANA], SEMANA)
    erros = {
        "holt_winters": mape(real, prev_teste),
        "semana_anterior": mape(real, preco[-2 * SEMANA:-SEMANA]),
        "ultimo_preco": mape(real, np.full(SEMANA, preco[-SEMANA - 1])),
    }
    previsto = holt_winters(log_p, SEMANA)

    agora_ts = t0 + (len(preco) - 1) * BLOCO
    nivel = ema(preco)
    return {
        "vendas_dia": round(float(vendas_dia), 1),
        "preco_atual": round(float(preco[-1])),
        "ema_3d": round(float(nivel)),
        "posicao": round(float(preco[-1] / nivel - 1), 4),
        "perfil": [round(float(v), 4) for v in perfil],
        "forca": round(float(forca), 3),
        "amplitude": round(float(perfil.max() - perfil.min()), 4),
        "faixa_compra": k_c, "faixa_venda": k_v,
        "margem_liquida": round(float(margem), 4),
        "proxima_compra": proxima(k_c, agora_ts),
        "faixa_atual": faixa_semana(agora_ts),
        "previsao": [{"ts": int(agora_ts + (i + 1) * BLOCO), "preco": round(float(v))}
                     for i, v in enumerate(previsto)],
        "erros": {k: round(v, 4) for k, v in erros.items()},
        "estrategia_teste": testar_estrategia(preco, log_p, t0),
        "ultimo_bloco": int(agora_ts),
    }


def sinal(r):
    """Leitura simples do momento: comprar perto do fundo, vender perto do pico."""
    perfil = r["perfil"]
    atual = perfil[r["faixa_atual"]]
    faixa = max(r["amplitude"], 1e-9)
    rel = (atual - min(perfil)) / faixa  # 0 = fundo da semana, 1 = pico
    if r["forca"] < 0.2 or r["margem_liquida"] <= 0:
        return "sem ciclo"
    # Só dá sinal se o ciclo se confirmou na semana de teste (fora do treino)
    # e a previsão sazonal foi melhor que repetir o último preço
    if not (r["estrategia_teste"] or 0) > 0 or r["erros"]["holt_winters"] >= r["erros"]["ultimo_preco"]:
        return "não confirmado"
    if rel <= 0.25 and r["posicao"] <= 0:
        return "comprar"
    if rel >= 0.75 and r["posicao"] >= 0:
        return "vender"
    return "aguardar"


def _tarefa(args):
    chave, vol, prata, t0 = args
    try:
        return chave, analisar_serie(vol, prata, t0)
    except (ValueError, np.linalg.LinAlgError):
        return chave, None


def main():
    inicio = time.time()
    ref = Referencia()
    grupos = grupos_dos_itens()
    con = sqlite3.connect(BANCO)
    saida = {"gerado_em": int(time.time()), "seg_ref": SEG_REF, "regioes": {}}
    pool = Pool(max(1, (os.cpu_count() or 2) - 1))

    for regiao in REGIOES:
        series = previsao.carregar_series(con, regiao)
        tarefas = [((item, loc), vol, prata, t0)
                   for (_, item, loc), (vol, prata, t0) in series.items()
                   if grupos.get(item) and loc in CIDADES]
        resultados, testes, erros_todos = [], [], []
        for (item, loc), r in pool.imap_unordered(_tarefa, tarefas, chunksize=16):
            grupo = grupos[item]
            if not r:
                continue
            info = ref.info(item)
            r.update(item=item, nome=info["nome"], tier=info["tier"], enc=info["enc"],
                     grupo=grupo, local=loc, cidade=CIDADES[loc])
            r["sinal"] = sinal(r)
            resultados.append(r)
            erros_todos.append(r["erros"])
            if r["estrategia_teste"] is not None and r["forca"] >= 0.2:
                testes.append(r["estrategia_teste"])

        # Curva semanal de cada grupo (média ponderada pela liquidez)
        curvas = {}
        for g in sorted({r["grupo"] for r in resultados}):
            membros = [r for r in resultados if r["grupo"] == g]
            pesos = np.array([np.sqrt(r["vendas_dia"]) for r in membros])
            perfis = np.array([r["perfil"] for r in membros])
            curva = (perfis * pesos[:, None]).sum(0) / pesos.sum()
            curvas[g] = {"perfil": [round(float(v), 4) for v in curva], "series": len(membros)}

        resumo = {}
        if erros_todos:
            for k in erros_todos[0]:
                resumo[f"erro_{k}"] = round(float(np.median([e[k] for e in erros_todos])), 4)
        if testes:
            t = np.array(testes)
            resumo.update(estrategia_series=len(t),
                          estrategia_acerto=round(float((t > 0).mean()), 3),
                          estrategia_mediana=round(float(np.median(t)), 4))
        saida["regioes"][regiao] = {"series": resultados, "grupos": curvas, "resumo": resumo}
        print(f"[{regiao}] {len(resultados)} séries analisadas · {resumo}")

    pool.close()
    ARQ_CICLOS.write_text(json.dumps(saida, ensure_ascii=False), encoding="utf-8")
    print(f"Gravado em {ARQ_CICLOS} ({time.time() - inicio:.0f} s)")


if __name__ == "__main__":
    main()
