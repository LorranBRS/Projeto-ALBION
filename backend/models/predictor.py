"""Pipeline de previsão de preço (Task 2).

Modelo tabular LightGBM, um por horizonte (12h e 24h):
  - alvo: log(preço daqui a h / preço atual)  -> sem escala, serve para itens de 10 ou 10 milhões
  - perda: MAE (objective="l1"), robusta a manipulações e outliers
  - intervalos: regressão quantílica (2,5% / 10% / 90% / 97,5%) calibrada por
    conformal (CQR) com os resíduos da última dobra da validação
  - validação: TimeSeriesSplit (walk-forward) sobre os instantes, com intervalo
    (gap) do tamanho do horizonte para não vazar o alvo

Uso:
    python -m backend.models.predictor          # treina, valida e grava dados/modelos_ml/
"""
import json
import os
import time

for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
from sklearn.model_selection import TimeSeriesSplit  # noqa: E402

from backend.analytics import features as F  # noqa: E402
from backend.services.signals import generate_trade_signals  # noqa: E402
from config import PASTA_DADOS  # noqa: E402
from itens import Referencia  # noqa: E402

PASTA_MODELOS = PASTA_DADOS / "modelos_ml"
ARQ_META = PASTA_MODELOS / "meta.json"
HORIZONTES = {"12h": 2, "24h": 4, "3d": 12, "7d": 28}   # nome -> blocos de 6h
QUANTIS = {"q025": 0.025, "q10": 0.10, "q90": 0.90, "q975": 0.975}
MIN_VENDAS_DIA = 5                                # liquidez mínima para treinar/prever
REGIAO_TREINO = "west"                            # região com mais dados

PARAMS = dict(
    n_estimators=400, learning_rate=0.05, num_leaves=63, min_child_samples=200,
    subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
    n_jobs=int(os.environ.get("OMP_NUM_THREADS", 4)), verbose=-1,
)


def _modelo(objetivo, alfa=None):
    p = dict(PARAMS, objective=objetivo)
    if alfa is not None:
        p["alpha"] = alfa
    return lgb.LGBMRegressor(**p)


def preparar_base(regiao=REGIAO_TREINO):
    ref = Referencia()
    df = F.carregar_historico(regiao)
    df = F.build_market_features(df, info_itens=ref.info)
    df = F.adicionar_alvos(df, horizontes=HORIZONTES)
    df = df[df["volume_rolling_7d"] >= MIN_VENDAS_DIA]
    return df


def _cobertura(y, lo, hi):
    return float(np.mean((y >= lo) & (y <= hi)))


def _ajuste_conformal(y, lo, hi, nivel):
    """CQR: quanto alargar o intervalo para cobrir `nivel` dos casos."""
    escores = np.maximum(lo - y, y - hi)
    n = len(escores)
    q = min(1.0, np.ceil((n + 1) * nivel) / n)
    return float(np.quantile(escores, q))


def validar_e_treinar(df, nome, passos, n_dobras=4):
    # Horizontes longos têm poucos pontos de partida com futuro conhecido
    if passos >= 12:
        n_dobras = 3
    alvo = f"target_{nome}"
    base = df.dropna(subset=[alvo]).sort_values("ts")
    X, y, ts = base[F.COLUNAS_MODELO], base[alvo].to_numpy(), base["ts"].to_numpy()
    instantes = np.unique(ts)
    if passos >= 28:
        # 7 dias: poucos pontos de partida com futuro conhecido (histórico curto);
        # 2 dobras com janelas de teste menores deixam treino suficiente
        tss = TimeSeriesSplit(n_splits=2, gap=passos, test_size=max(8, len(instantes) // 7))
    else:
        tss = TimeSeriesSplit(n_splits=n_dobras, gap=passos)

    dobras = []
    for i, (idx_tr, idx_te) in enumerate(tss.split(instantes)):
        tr = np.isin(ts, instantes[idx_tr])
        te = np.isin(ts, instantes[idx_te])
        pontual = _modelo("l1").fit(X[tr], y[tr])
        pred = pontual.predict(X[te])
        q = {k: _modelo("quantile", a).fit(X[tr], y[tr]).predict(X[te]) for k, a in QUANTIS.items()}
        yt = y[te]
        mae = float(np.mean(np.abs(yt - pred)))
        mae_ingenuo = float(np.mean(np.abs(yt)))  # "o preço não muda"
        dobra = {
            "dobra": i + 1, "linhas_teste": int(te.sum()),
            "mae_log": round(mae, 5), "mae_log_ingenuo": round(mae_ingenuo, 5),
            "ganho_vs_ingenuo": round(1 - mae / mae_ingenuo, 4),
            # Só conta os casos em que o preço de fato mudou
            "acerto_direcao": round(float(np.mean((np.sign(pred) == np.sign(yt))[yt != 0])), 4),
            "cobertura_80": round(_cobertura(yt, q["q10"], q["q90"]), 4),
            "cobertura_95": round(_cobertura(yt, q["q025"], q["q975"]), 4),
        }
        dobras.append(dobra)
        ultima = (base[te], yt, pred, q)
        print(f"  [{nome}] dobra {i + 1}: {dobra}")

    # Calibração conformal com a última dobra (a mais recente)
    _, yt, _, q = ultima
    ajuste = {
        "80": _ajuste_conformal(yt, q["q10"], q["q90"], 0.80),
        "95": _ajuste_conformal(yt, q["q025"], q["q975"], 0.95),
    }

    # Teste dos sinais de compra/venda na última dobra (fora do treino)
    sinais = testar_sinais(ultima, ajuste)

    # Modelos finais com todos os dados
    modelos = {"ponto": _modelo("l1").fit(X, y)}
    for k, a in QUANTIS.items():
        modelos[k] = _modelo("quantile", a).fit(X, y)
    PASTA_MODELOS.mkdir(parents=True, exist_ok=True)
    for k, m in modelos.items():
        m.booster_.save_model(str(PASTA_MODELOS / f"{nome}_{k}.txt"))

    importancia = dict(zip(F.COLUNAS_MODELO,
                           modelos["ponto"].booster_.feature_importance("gain").round(1).tolist()))
    return {
        "dobras": dobras, "ajuste_conformal": ajuste, "sinais_teste": sinais,
        "linhas_treino": int(len(base)), "importancia": importancia,
    }


def testar_sinais(ultima, ajuste):
    """Aplica as regras de sinal nas previsões fora da amostra e mede o resultado real."""
    base, yt, pred, q = ultima
    precos = base["price"].to_numpy()
    z = base["z_score_14d"].to_numpy()
    resultados = {"COMPRA": [], "VENDA": []}
    for i in range(len(yt)):
        lo = precos[i] * np.exp(q["q10"][i] - ajuste["80"])
        hi = precos[i] * np.exp(q["q90"][i] + ajuste["80"])
        s = generate_trade_signals({
            "preco_atual": precos[i], "price_pred_12h": precos[i] * np.exp(pred[i]),
            "price_pred_lower": lo, "price_pred_upper": hi, "z_score": z[i],
        })["sinal"]
        if s in resultados:
            futuro = precos[i] * np.exp(yt[i])
            if s == "COMPRA":   # comprar agora e vender depois pagando as taxas
                resultados[s].append(futuro * (1 - F.TAXA_VENDA) / precos[i] - 1)
            else:               # vender agora (com taxa) em vez de vender depois
                resultados[s].append(precos[i] * (1 - F.TAXA_VENDA) / (futuro * (1 - F.TAXA_VENDA)) - 1)
    resumo = {}
    for s, r in resultados.items():
        r = np.array(r)
        resumo[s] = {
            "quantidade": int(len(r)),
            "acerto": round(float((r > 0).mean()), 4) if len(r) else None,
            "resultado_mediano": round(float(np.median(r)), 4) if len(r) else None,
        }
    return resumo


def main(apenas=None):
    """Treina todos os horizontes, ou só os pedidos (ex.: python -m backend.models.predictor 7d)."""
    inicio = time.time()
    print("Preparando base de treino...")
    df = preparar_base()
    meta = {"horizontes": {}}
    if apenas and ARQ_META.exists():
        meta = json.loads(ARQ_META.read_text(encoding="utf-8"))
    meta.update(treinado_em=int(time.time()), regiao_treino=REGIAO_TREINO, colunas=F.COLUNAS_MODELO)
    for nome, passos in HORIZONTES.items():
        if apenas and nome not in apenas:
            continue
        print(f"Horizonte {nome}:")
        meta["horizontes"][nome] = validar_e_treinar(df, nome, passos)
    PASTA_MODELOS.mkdir(parents=True, exist_ok=True)
    ARQ_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v["sinais_teste"] for k, v in meta["horizontes"].items()}, indent=1))
    print(f"Modelos gravados em {PASTA_MODELOS} ({time.time() - inicio:.0f} s)")


# --- inferência ---------------------------------------------------------------
class Preditor:
    """Carrega os modelos salvos e faz previsões com intervalo de confiança."""

    def __init__(self):
        self.meta = json.loads(ARQ_META.read_text(encoding="utf-8"))
        self.modelos = {
            nome: {k: lgb.Booster(model_file=str(PASTA_MODELOS / f"{nome}_{k}.txt"))
                   for k in ["ponto", *QUANTIS]}
            for nome in HORIZONTES
        }

    def prever(self, linhas):
        """Recebe um DataFrame de features e devolve preços previstos e intervalos."""
        X = linhas[F.COLUNAS_MODELO]
        preco = linhas["price"].to_numpy()
        saida = {}
        for nome, m in self.modelos.items():
            aj = self.meta["horizontes"][nome]["ajuste_conformal"]
            r = {k: m[k].predict(X) for k in m}
            saida[nome] = {
                "price_pred": preco * np.exp(r["ponto"]),
                "price_pred_lower": preco * np.exp(r["q10"] - aj["80"]),
                "price_pred_upper": preco * np.exp(r["q90"] + aj["80"]),
                "price_pred_lower_95": preco * np.exp(r["q025"] - aj["95"]),
                "price_pred_upper_95": preco * np.exp(r["q975"] + aj["95"]),
            }
        return saida


if __name__ == "__main__":
    import sys
    main(set(sys.argv[1:]) or None)
