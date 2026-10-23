"""Motor de decisão: transforma a previsão em sinal de compra/venda (Task 3).

Regras (taxa de mercado = 6,5%: 4% de imposto com premium + 2,5% de anúncio):
  COMPRA  se preço_atual < (limite_inferior − taxas) e z-score < −1,5
  VENDA   se preço_atual > (limite_superior − taxas) e z-score > +1,5
  NEUTRO  caso contrário (o preço está dentro da margem de ruído)

As taxas são calculadas sobre o próprio limite (ex.: limite_inferior × 6,5%).
O intervalo usado é o de 80% da previsão de 12h.
"""
import math

TAXA_MERCADO = 0.065
Z_COMPRA = -1.5
Z_VENDA = 1.5


def generate_trade_signals(item_data, taxa=TAXA_MERCADO):
    """item_data: preco_atual, price_pred_12h, price_pred_lower, price_pred_upper, z_score."""
    atual = item_data.get("preco_atual")
    lo = item_data.get("price_pred_lower")
    hi = item_data.get("price_pred_upper")
    z = item_data.get("z_score")
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (atual, lo, hi, z)):
        return {"sinal": "SEM DADOS", "motivo": "histórico insuficiente para o z-score ou a previsão"}

    limite_compra = lo - lo * taxa
    limite_venda = hi - hi * taxa
    if atual < limite_compra and z < Z_COMPRA:
        return {"sinal": "COMPRA",
                "motivo": f"preço abaixo do limite inferior previsto já descontando {taxa:.1%} de taxas, "
                          f"e {abs(z):.1f} desvios padrão abaixo da média de 14 dias"}
    if atual > limite_venda and z > Z_VENDA:
        return {"sinal": "VENDA",
                "motivo": f"preço acima do limite superior previsto (menos taxas) "
                          f"e {z:.1f} desvios padrão acima da média de 14 dias"}
    return {"sinal": "NEUTRO", "motivo": "preço dentro da margem de ruído estatístico"}
