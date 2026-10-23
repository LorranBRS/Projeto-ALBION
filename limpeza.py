"""Limpeza dos dados de preço: descarta picos fora do padrão.

Jogadores podem manipular o histórico vendendo um item de 10 mil por 10
milhões (ou por 1 prata). Cada ponto de histórico é comparado com a mediana
de preço do mesmo item, cidade e qualidade; o que passar de FATOR_PICO vezes
acima ou abaixo dela é ignorado. A mediana não é afetada por poucos picos.
"""
import statistics

FATOR_PICO = 3.0
MIN_PONTOS_REFERENCIA = 3   # com menos pontos não dá para saber o que é normal
VARIACAO_MINIMA = 0.01      # preço que não varia mais que 1% em 7 dias = "parado"


def referencia(precos):
    """Mediana dos preços, ou None se houver poucos pontos."""
    if len(precos) < MIN_PONTOS_REFERENCIA:
        return None
    return statistics.median(precos)


def dentro_do_padrao(preco, ref):
    return ref is None or ref / FATOR_PICO <= preco <= ref * FATOR_PICO


def preco_parado(precos):
    """True se há pontos suficientes e o preço praticamente não mudou."""
    if len(precos) < 8:
        return False
    return max(precos) / min(precos) - 1 < VARIACAO_MINIMA
