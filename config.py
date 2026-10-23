"""Configurações compartilhadas entre o coletor e o servidor."""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PASTA_DADOS = RAIZ / "dados"
BANCO = PASTA_DADOS / "mercado.db"

NATS_USUARIO = "public:thenewalbiondata"
NATS_HOST = "nats.albion-online-data.com"

# Servidores do jogo -> porta NATS
REGIOES = {
    "west": {"nome": "Américas", "porta": 4222},
    "europe": {"nome": "Europa", "porta": 34222},
    "east": {"nome": "Ásia", "porta": 24222},
}

TOPICOS = ["marketorders.deduped", "markethistories.deduped", "goldprices.deduped"]

# Escalas do histórico de mercado (campo Timescale)
ESCALA_HORA = 0   # pontos de 1h, últimas 24h
ESCALA_6H_7D = 1  # pontos de 6h, últimos 7 dias
ESCALA_6H_28D = 2  # pontos de 6h, últimas 4 semanas

# Retenção dos dados no banco
RETENCAO_ORDENS_H = 48
RETENCAO_HISTORICO_D = 180  # ciclos semanais precisam de muitas semanas

# Conversão de ticks do C# para epoch (segundos)
TICKS_EPOCH = 621355968000000000


def ticks_para_epoch(ticks: int) -> int:
    return (ticks - TICKS_EPOCH) // 10_000_000
