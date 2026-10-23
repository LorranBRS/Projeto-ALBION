"""Previsão de tendência de preço para as próximas horas.

Modelo simples e auditável: regressão linear (ridge) sobre indicadores do
histórico em blocos de 6h (últimos 28 dias). Prevê a variação do preço médio
nas próximas 12h em relação às últimas 24h.

Indicadores (por item, cidade, qualidade Normal):
    momento_curto  preço do último bloco x média dos 3 anteriores
    momento_24h    preço médio 24h x preço médio dos 6 dias anteriores
    volume         vendas 24h x média diária dos 6 dias anteriores
    desvio         preço 24h x mediana de 7 dias

Uso:
    python previsao.py            # treina, testa e grava dados/modelo.json
"""
import json
import math
import sqlite3
import time
from collections import defaultdict

import numpy as np

import limpeza
from config import BANCO, ESCALA_6H_28D, PASTA_DADOS

ARQ_MODELO = PASTA_DADOS / "modelo.json"
BLOCO = 6 * 3600
JANELA = 28            # blocos de histórico usados nos indicadores (7 dias)
HORIZONTE = 2          # blocos à frente (12h)
MIN_VENDAS_24H = 10    # liquidez mínima para prever
DIAS_TESTE = 7         # últimos dias separados para medir acerto
# Para entrar no top 3 publicado: liquidez e preço sem anomalia
TOP_MIN_VENDAS_24H = 50
TOP_MAX_DESVIO = 0.35  # preço 24h até ~30% abaixo/acima da mediana de 7 dias
NOMES = ["momento_curto", "momento_24h", "volume", "desvio"]


def carregar_series(con, regiao=None, ate=None, itens=None):
    """Devolve {(regiao, item, local): (precos[], volumes[], t0)} em grade de 6h."""
    ate = ate or int(time.time())
    # O bloco atual ainda está incompleto, então fica de fora
    ultimo = (ate // BLOCO) * BLOCO - BLOCO
    sql = f"""SELECT regiao, item, local, ts, quantidade, prata FROM historico
              WHERE escala={ESCALA_6H_28D} AND qualidade=1 AND ts<=?"""
    args = [ultimo]
    if regiao:
        sql += " AND regiao=?"
        args.append(regiao)
    if itens:
        # O "+" impede o SQLite de usar o índice (regiao, escala, ts), que varre
        # a região inteira; assim ele usa a chave primária (regiao, item, ...)
        sql = sql.replace("escala=", "+escala=").replace("ts<=", "+ts<=")
        sql += f" AND item IN ({','.join('?' * len(itens))})"
        args.extend(itens)
    brutos = defaultdict(dict)
    for r, item, loc, ts, q, p in con.execute(sql, args):
        if q > 0:
            brutos[(r, item, loc)][ts // BLOCO] = (q, p)
    series = {}
    fim = ultimo // BLOCO
    for chave, pontos in brutos.items():
        inicio = min(pontos)
        n = fim - inicio + 1
        if n < JANELA + 1:
            continue
        vol = np.zeros(n)
        prata = np.zeros(n)
        # Picos fora do padrão (manipulação) são tratados como bloco sem venda
        ref = limpeza.referencia([p / q for q, p in pontos.values()])
        for b, (q, p) in pontos.items():
            if limpeza.dentro_do_padrao(p / q, ref):
                vol[b - inicio], prata[b - inicio] = q, p
        series[chave] = (vol, prata, inicio * BLOCO)
    return series


def _media(vol, prata, a, b):
    v = vol[a:b].sum()
    return prata[a:b].sum() / v if v else None


def indicadores(vol, prata, t):
    """Indicadores usando só os blocos até t (inclusive). None se faltar dado."""
    if t < JANELA - 1:
        return None
    v24 = vol[t - 3:t + 1].sum()
    if v24 < MIN_VENDAS_24H or vol[t] == 0:
        return None
    p_ult = prata[t] / vol[t]
    p_ant = _media(vol, prata, t - 3, t)
    p24 = _media(vol, prata, t - 3, t + 1)
    p_base = _media(vol, prata, t - JANELA + 1, t - 3)
    if not (p_ant and p24 and p_base):
        return None
    precos = [prata[i] / vol[i] for i in range(t - JANELA + 1, t + 1) if vol[i]]
    mediana = float(np.median(precos))
    v_base_dia = vol[t - JANELA + 1:t - 3].sum() / ((JANELA - 4) / 4)
    return [
        math.log(p_ult / p_ant),
        math.log(p24 / p_base),
        math.log((v24 + 1) / (v_base_dia + 1)),
        math.log(p24 / mediana),
    ], p24, v24


def elegivel_top(ind, v24):
    return v24 >= TOP_MIN_VENDAS_24H and abs(ind[3]) <= TOP_MAX_DESVIO


def alvo(vol, prata, t, p24):
    """Variação do preço médio nas próximas 12h contra as últimas 24h."""
    p_fut = _media(vol, prata, t + 1, t + 1 + HORIZONTE)
    return math.log(p_fut / p24) if p_fut else None


def montar_base(series):
    X, y, quando, elegivel = [], [], [], []
    for vol, prata, t0 in series.values():
        for t in range(JANELA - 1, len(vol) - HORIZONTE):
            ind = indicadores(vol, prata, t)
            if not ind:
                continue
            a = alvo(vol, prata, t, ind[1])
            if a is None:
                continue
            X.append(ind[0])
            y.append(a)
            quando.append(t0 + t * BLOCO)
            elegivel.append(elegivel_top(ind[0], ind[2]))
    return np.array(X), np.array(y), np.array(quando), np.array(elegivel)


def treinar(X, y, ridge=1.0):
    # Corta extremos (erros de digitação de preço no jogo distorcem tudo)
    X = np.clip(X, -2, 2)
    y = np.clip(y, -1, 1)
    media, desvio = X.mean(0), X.std(0) + 1e-9
    Z = (X - media) / desvio
    A = np.hstack([Z, np.ones((len(Z), 1))])
    reg = ridge * np.eye(A.shape[1])
    reg[-1, -1] = 0
    coef = np.linalg.solve(A.T @ A + reg, A.T @ y)
    return {"media": media.tolist(), "desvio": desvio.tolist(),
            "pesos": coef[:-1].tolist(), "intercepto": float(coef[-1])}


def prever(modelo, X):
    X = np.clip(np.asarray(X, dtype=float), -2, 2)
    Z = (X - modelo["media"]) / np.array(modelo["desvio"])
    return Z @ np.array(modelo["pesos"]) + modelo["intercepto"]


def avaliar(modelo, X, y, quando, elegivel):
    """Mede o acerto como o site usa: os 3 maiores 'vai subir' de cada momento."""
    pred = prever(modelo, X)
    acertos_top = total_top = 0
    altas_top = []
    for t in np.unique(quando):
        idx = np.where((quando == t) & elegivel)[0]
        if len(idx) < 20:
            continue
        top = idx[np.argsort(-pred[idx])[:3]]
        top = top[pred[top] > 0]
        acertos_top += int((y[top] > 0).sum())
        total_top += len(top)
        altas_top.extend(np.exp(y[top]) - 1)
    return {
        "amostras_teste": int(len(y)),
        "acerto_direcao_geral": round(float(((pred > 0) == (y > 0)).mean()), 3),
        "correlacao": round(float(np.corrcoef(pred, y)[0, 1]), 3),
        "acerto_top3": round(acertos_top / total_top, 3) if total_top else None,
        "previsoes_top3": total_top,
        "alta_mediana_top3": round(float(np.median(altas_top)), 4) if altas_top else None,
        "alta_media_top3": round(float(np.mean(altas_top)), 4) if altas_top else None,
        "subiu_sem_modelo": round(float((y > 0).mean()), 3),
    }


def main():
    con = sqlite3.connect(BANCO)
    series = carregar_series(con)
    X, y, quando, elegivel = montar_base(series)
    print(f"{len(series)} séries, {len(y)} exemplos")
    corte = quando.max() - DIAS_TESTE * 86400
    treino, teste = quando <= corte - HORIZONTE * BLOCO, quando > corte
    modelo = treinar(X[treino], y[treino])
    teste_res = avaliar(modelo, X[teste], y[teste], quando[teste],
                        elegivel[teste])
    print("Pesos:", dict(zip(NOMES, [round(p, 4) for p in modelo["pesos"]])))
    print("Teste (últimos 7 dias, fora do treino):", teste_res)

    # Modelo final treinado com tudo, mas o acerto publicado é o do teste
    final = treinar(X, y)
    final.update(indicadores=NOMES, teste=teste_res, treinado_em=int(time.time()),
                 horizonte_h=HORIZONTE * 6)
    ARQ_MODELO.write_text(json.dumps(final, indent=2), encoding="utf-8")
    print(f"Modelo gravado em {ARQ_MODELO}")


if __name__ == "__main__":
    main()
