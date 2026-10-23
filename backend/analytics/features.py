"""Engenharia de features do mercado (Task 1).

Base temporal: blocos de 6h do histórico do AODP (escala 2, últimos 28 dias),
qualidade Normal, já sem picos de manipulação (limpeza.py). O "preço" de cada
bloco é o preço médio negociado: o AODP não guarda histórico do menor preço de
venda nem do maior de compra, só o estado atual das ordens. Por isso:

  - `price` (preço médio negociado) faz o papel de `sell_price_min` nas séries;
  - `spread_relative` usa as ordens atuais e só existe para o momento presente;
  - `lag_1h` não existe na resolução de 6h; o menor lag é `lag_6h`.
"""
import sqlite3
import time

import numpy as np
import pandas as pd

import previsao
from config import BANCO

BLOCO_H = 6
POR_DIA = 24 // BLOCO_H          # 4 blocos por dia
JANELA_7D, JANELA_14D, JANELA_30D = 7 * POR_DIA, 14 * POR_DIA, 30 * POR_DIA
TAXA_VENDA = 0.065               # 4% de imposto (premium) + 2,5% de anúncio

# Colunas usadas pelo modelo (todas sem escala: servem para itens baratos e caros)
COLUNAS_MODELO = [
    "pct_change_6h", "pct_change_12h", "pct_change_24h", "pct_change_7d",
    "z_score_14d", "dist_sma_7d", "cv_7d",
    "log_volume", "volume_ratio_7d_30d",
    "sin_day_of_week", "cos_day_of_week", "sin_hour", "cos_hour",
    "tier", "enc",
]


def carregar_historico(regiao, itens=None, con=None):
    """DataFrame com uma linha por (item, local, bloco de 6h).

    Colunas: item, local, ts, price, volume. Blocos sem venda ficam com
    volume 0 e preço preenchido com o último conhecido (marcado em `has_sale`).
    """
    fechar = con is None
    con = con or sqlite3.connect(BANCO)
    series = previsao.carregar_series(con, regiao, itens=sorted(itens) if itens else None)
    if fechar:
        con.close()
    partes = []
    for (_, item, loc), (vol, prata, t0) in series.items():
        if itens is not None and item not in itens:
            continue
        n = len(vol)
        preco = np.where(vol > 0, prata / np.maximum(vol, 1), np.nan)
        partes.append(pd.DataFrame({
            "item": item, "local": loc,
            "ts": t0 + np.arange(n) * BLOCO_H * 3600,
            "price": preco, "volume": vol,
        }))
    if not partes:
        return pd.DataFrame(columns=["item", "local", "ts", "price", "volume"])
    df = pd.concat(partes, ignore_index=True)
    df["has_sale"] = df["volume"] > 0
    df["price"] = df.groupby(["item", "local"])["price"].ffill()
    return df.dropna(subset=["price"]).reset_index(drop=True)


def ordens_atuais(regiao, con=None, janela_h=6, itens=None):
    """Menor venda e maior compra atuais por (item, local), qualidade Normal."""
    fechar = con is None
    con = con or sqlite3.connect(BANCO)
    agora = int(time.time())
    filtro, args = "", []
    if itens:
        filtro = f"AND item IN ({','.join('?' * len(itens))})"
        args = sorted(itens)
    df = pd.read_sql_query(f"""
        SELECT item, local,
            MIN(CASE WHEN tipo='offer' THEN preco END) sell_price_min,
            MAX(CASE WHEN tipo='request' THEN preco END) buy_price_max
        FROM ordens WHERE regiao=? AND qualidade=1 AND ultima_vez>=? {filtro}
        GROUP BY item, local""", con, params=(regiao, agora - janela_h * 3600, *args))
    if fechar:
        con.close()
    return df


def _rolar(df, coluna, janela, minimo, func):
    """Janela móvel por (item, local), vetorizada (bem mais rápida que transform+lambda)."""
    r = df.groupby(["item", "local"], sort=False)[coluna].rolling(janela, min_periods=minimo)
    return getattr(r, func)().reset_index(level=[0, 1], drop=True)


def build_market_features(df, info_itens=None, ordens=None, custos=None):
    """Recebe o DataFrame bruto (item, local, ts, price, volume) e devolve as features.

    - Liquidez & spread: spread_relative (com `ordens`), volume_rolling_7d/30d, price_std_7d
    - Z-score: z_score_14d = (preço − SMA 14d) / desvio 14d
    - Sazonalidade: sin/cos do dia da semana e da hora
    - Custo teórico (com `custos`): cost_theoretical e crafting_margin_ratio
    - Lags e retornos: lag_6h, lag_12h, lag_24h, lag_7d, pct_change_*
    """
    df = df.sort_values(["item", "local", "ts"]).copy()
    g = df.groupby(["item", "local"], sort=False)

    # --- Liquidez & spread ---
    df["volume_rolling_7d"] = _rolar(df, "volume", JANELA_7D, POR_DIA, "mean") * POR_DIA
    df["volume_rolling_30d"] = _rolar(df, "volume", JANELA_30D, JANELA_7D, "mean") * POR_DIA
    df["price_std_7d"] = _rolar(df, "price", JANELA_7D, POR_DIA * 2, "std")
    sma_7d = _rolar(df, "price", JANELA_7D, POR_DIA * 2, "mean")

    # --- Z-score 14 dias ---
    sma_14d = _rolar(df, "price", JANELA_14D, JANELA_7D, "mean")
    std_14d = _rolar(df, "price", JANELA_14D, JANELA_7D, "std")
    df["sma_14d"] = sma_14d
    df["price_std_14d"] = std_14d
    df["z_score_14d"] = (df["price"] - sma_14d) / std_14d.replace(0, np.nan)

    # --- Sazonalidade temporal (UTC) ---
    dt = pd.to_datetime(df["ts"], unit="s", utc=True)
    dia = dt.dt.dayofweek
    hora = dt.dt.hour
    df["sin_day_of_week"] = np.sin(2 * np.pi * dia / 7)
    df["cos_day_of_week"] = np.cos(2 * np.pi * dia / 7)
    df["sin_hour"] = np.sin(2 * np.pi * hora / 24)
    df["cos_hour"] = np.cos(2 * np.pi * hora / 24)

    # --- Lags & retornos ---
    for nome, passos in (("6h", 1), ("12h", 2), ("24h", 4), ("7d", JANELA_7D)):
        df[f"lag_{nome}"] = g["price"].shift(passos)
        df[f"pct_change_{nome}"] = df["price"] / df[f"lag_{nome}"] - 1

    # --- Volatilidade semanal (para a projeção de 4 semanas) ---
    df["log_ret_7d"] = np.log(df["price"] / df["lag_7d"])
    df["vol_semanal"] = _rolar(df, "log_ret_7d", JANELA_30D, POR_DIA * 3, "std")

    # --- Derivadas usadas pelo modelo ---
    df["dist_sma_7d"] = df["price"] / sma_7d - 1
    df["cv_7d"] = df["price_std_7d"] / sma_7d
    df["log_volume"] = np.log1p(df["volume_rolling_7d"])
    df["volume_ratio_7d_30d"] = df["volume_rolling_7d"] / df["volume_rolling_30d"]

    if info_itens is not None:
        unicos = df["item"].unique()
        df["tier"] = df["item"].map({i: info_itens(i)["tier"] for i in unicos})
        df["enc"] = df["item"].map({i: info_itens(i)["enc"] for i in unicos})
    else:
        df["tier"] = df["item"].str.extract(r"^T(\d)", expand=False).astype(float)
        df["enc"] = df["item"].str.extract(r"@(\d)$", expand=False).fillna(0).astype(float)

    # --- Spread atual (só existe para o presente) ---
    if ordens is not None and len(ordens):
        df = df.merge(ordens, on=["item", "local"], how="left")
        df["spread_relative"] = (df["sell_price_min"] - df["buy_price_max"]) / df["sell_price_min"]
    else:
        df["sell_price_min"] = np.nan
        df["buy_price_max"] = np.nan
        df["spread_relative"] = np.nan

    # --- Custo teórico de fabricação ---
    if custos is not None:
        df["cost_theoretical"] = df["item"].map(custos)
        df["crafting_margin_ratio"] = df["price"] / df["cost_theoretical"]
    else:
        df["cost_theoretical"] = np.nan
        df["crafting_margin_ratio"] = np.nan

    return df.replace([np.inf, -np.inf], np.nan)


def adicionar_alvos(df, horizontes=None):
    """Alvos target_<nome> = log(preço futuro / preço atual), ex.: {"12h": 2, "7d": 28} (blocos de 6h).

    Só vale quando houve venda de verdade no bloco futuro (não preço repetido).
    """
    horizontes = horizontes or {"12h": 2, "24h": 4}
    g = df.groupby(["item", "local"], sort=False)
    for nome, passos in horizontes.items():
        futuro = g["price"].shift(-passos)
        teve_venda = g["has_sale"].shift(-passos).astype("boolean").fillna(False).astype(bool)
        alvo = np.log(futuro / df["price"])
        df[f"target_{nome}"] = alvo.where(teve_venda & df["has_sale"])
    return df


def custo_teorico(rec, precos, rrf):
    """sum(qtd × preço) × (1 − RRF) dos materiais que retornam + os que não retornam
    (artefatos, tokens) + a prata cobrada pela receita, por unidade fabricada."""
    ret = fixo = 0.0
    for r in rec["recursos"]:
        p = precos.get(r["id"])
        if not p:
            return None
        if r["retorna"]:
            ret += r["qtd"] * p
        else:
            fixo += r["qtd"] * p
    return (ret * (1 - rrf) + fixo + rec.get("prata", 0)) / rec["qtd_fabricada"]
