"""Carga inicial pela API REST: preenche o histórico de todos os itens.

O NATS só traz o histórico de um item quando algum jogador abre a aba de
histórico dele no jogo, então logo após ligar o coletor o ranking fica vazio.
Este script busca as últimas 24h (por hora) e os últimos 28 dias (blocos de 6h)
de todos os itens, devagar, para não ser bloqueado pela API.

Uso:
    python carga_inicial.py               # todas as regiões
    python carga_inicial.py west          # só uma região
"""
import logging
import sys
import time
from datetime import datetime, timezone

import httpx

from config import ESCALA_6H_7D, ESCALA_6H_28D, ESCALA_HORA, REGIOES
from coletor import abrir_banco
from itens import CIDADES, Referencia

log = logging.getLogger("carga")

HOSTS = {
    "west": "https://west.albion-online-data.com",
    "europe": "https://europe.albion-online-data.com",
    "east": "https://east.albion-online-data.com",
}
ITENS_POR_LOTE = 100
PAUSA = 3  # segundos entre requisições (a API bloqueia quem abusa)

# A API REST devolve o nome do local; o NATS usa o id numérico
LOCAL_POR_NOME = {nome.lower(): loc for loc, nome in CIDADES.items()}
LOCAL_POR_NOME["black market"] = 3003


def epoch(texto):
    return int(datetime.fromisoformat(texto).replace(tzinfo=timezone.utc).timestamp())


class Carga:
    def __init__(self):
        self.ref = Referencia()
        self.con = abrir_banco()
        self.http = httpx.Client(timeout=60, headers={
            "Accept-Encoding": "gzip",
            "User-Agent": "albion-em-alta (site de ranking de mercado)",
        })

    def buscar(self, regiao, ids, escala_api):
        url = f"{HOSTS[regiao]}/api/v2/stats/history/{','.join(ids)}"
        for tentativa in range(5):
            r = self.http.get(url, params={"time-scale": escala_api})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                reset = int(r.headers.get("ratelimit-reset", 0))
                espera = max(60, reset - int(time.time()) + 5) if reset else 60
                log.warning("Limite da API atingido, aguardando %d s", espera)
                time.sleep(espera)
                continue
            log.warning("HTTP %s, nova tentativa em 10 s", r.status_code)
            time.sleep(10)
        return []

    def gravar(self, regiao, dados, escalas, desde):
        agora = int(time.time())
        linhas = []
        for d in dados:
            loc = LOCAL_POR_NOME.get(d["location"].lower())
            if loc is None:
                continue
            for p in d["data"]:
                ts = epoch(p["timestamp"])
                if ts < desde:
                    continue
                for esc in escalas:
                    linhas.append((regiao, d["item_id"], loc, d["quality"], esc,
                                   ts, p["item_count"],
                                   p["avg_price"] * p["item_count"], agora))
        # Não sobrescreve pontos que já vieram pelo NATS (são mais recentes)
        self.con.executemany(
            "INSERT OR IGNORE INTO historico VALUES (?,?,?,?,?,?,?,?,?)", linhas)
        self.con.commit()
        return len(linhas)

    def rodar(self, regiao):
        ids = sorted(self.ref.itens)
        lotes = [ids[i:i + ITENS_POR_LOTE] for i in range(0, len(ids), ITENS_POR_LOTE)]
        agora = int(time.time())
        total = 0
        log.info("[%s] %d itens em %d lotes (~%d min)", regiao, len(ids),
                 len(lotes), len(lotes) * 2 * PAUSA // 60)
        for n, lote in enumerate(lotes, 1):
            hora = self.buscar(regiao, lote, 1)
            total += self.gravar(regiao, hora, [ESCALA_HORA], agora - 86400)
            time.sleep(PAUSA)
            seis = self.buscar(regiao, lote, 6)
            total += self.gravar(regiao, seis, [ESCALA_6H_28D], agora - 28 * 86400)
            total += self.gravar(regiao, seis, [ESCALA_6H_7D], agora - 8 * 86400)
            time.sleep(PAUSA)
            if n % 10 == 0 or n == len(lotes):
                log.info("[%s] lote %d/%d, %d pontos gravados", regiao, n,
                         len(lotes), total)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    regioes = sys.argv[1:] or list(REGIOES)
    carga = Carga()
    for r in regioes:
        if r not in HOSTS:
            sys.exit(f"Região inválida: {r}. Use: {list(HOSTS)}")
        carga.rodar(r)


if __name__ == "__main__":
    main()
