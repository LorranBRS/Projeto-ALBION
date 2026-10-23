"""Coletor em tempo real: assina o NATS do Albion Data Project e grava no SQLite.

Uso:
    python coletor.py                 # todas as regiões
    python coletor.py west europe     # só as regiões escolhidas
"""
import asyncio
import json
import logging
import sqlite3
import sys
import time

import nats

from config import (BANCO, NATS_HOST, NATS_USUARIO, PASTA_DADOS, REGIOES,
                    RETENCAO_HISTORICO_D, RETENCAO_ORDENS_H, TOPICOS,
                    ticks_para_epoch)
from itens import Referencia

log = logging.getLogger("coletor")

INTERVALO_GRAVACAO = 5      # segundos entre gravações em lote
INTERVALO_LIMPEZA = 3600    # segundos entre limpezas de dados antigos

ESQUEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS ordens (
    regiao TEXT, id INTEGER, item TEXT, local INTEGER, qualidade INTEGER,
    preco INTEGER, quantidade INTEGER, tipo TEXT, expira TEXT,
    primeira_vez INTEGER, ultima_vez INTEGER,
    PRIMARY KEY (regiao, id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_ordens_item ON ordens (regiao, item, ultima_vez);
CREATE INDEX IF NOT EXISTS ix_ordens_primeira ON ordens (regiao, primeira_vez);

CREATE TABLE IF NOT EXISTS historico (
    regiao TEXT, item TEXT, local INTEGER, qualidade INTEGER, escala INTEGER,
    ts INTEGER, quantidade INTEGER, prata INTEGER, recebido INTEGER,
    PRIMARY KEY (regiao, item, local, qualidade, escala, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_hist_regiao_ts ON historico (regiao, escala, ts);

CREATE TABLE IF NOT EXISTS ouro (
    regiao TEXT, ts INTEGER, preco INTEGER, PRIMARY KEY (regiao, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS status (
    regiao TEXT PRIMARY KEY, inicio INTEGER, ultima_msg INTEGER,
    msgs_ordens INTEGER DEFAULT 0, msgs_historico INTEGER DEFAULT 0
);
"""


def abrir_banco():
    PASTA_DADOS.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(BANCO, timeout=30)
    con.executescript(ESQUEMA)
    return con


class Coletor:
    def __init__(self, regioes):
        self.regioes = regioes
        self.ref = Referencia()
        self.con = abrir_banco()
        self.ordens = []
        self.historico = []
        self.ouro = []
        self.contagem = {r: [0, 0] for r in regioes}  # [ordens, historicos]
        self.desconhecidos = set()
        agora = int(time.time())
        for r in regioes:
            self.con.execute(
                "INSERT INTO status (regiao, inicio) VALUES (?, ?) "
                "ON CONFLICT(regiao) DO UPDATE SET inicio=excluded.inicio",
                (r, agora))
        self.con.commit()

    # --- tratamento das mensagens -------------------------------------
    def ao_receber(self, regiao, msg):
        try:
            dados = json.loads(msg.data)
        except ValueError:
            return
        agora = int(time.time())
        if msg.subject == "marketorders.deduped":
            self._ordem(regiao, dados, agora)
        elif msg.subject == "markethistories.deduped":
            self._historico(regiao, dados, agora)
        elif msg.subject == "goldprices.deduped":
            self._ouro(regiao, dados)

    def _ordem(self, regiao, o, agora):
        loc, _ = self.ref.local_normalizado(o["LocationId"])
        self.ordens.append((
            regiao, o["Id"], o["ItemTypeId"], loc, o["QualityLevel"],
            o["UnitPriceSilver"], o["Amount"], o["AuctionType"],
            o.get("Expires"), agora, agora))
        self.contagem[regiao][0] += 1

    def _historico(self, regiao, h, agora):
        item = self.ref.codigo_por_indice(h["AlbionId"])
        if not item:
            if h["AlbionId"] not in self.desconhecidos:
                self.desconhecidos.add(h["AlbionId"])
                log.warning("AlbionId desconhecido: %s (atualize o cache com "
                            "'python itens.py')", h["AlbionId"])
            return
        loc, _ = self.ref.local_normalizado(h["LocationId"])
        for p in h["MarketHistories"]:
            self.historico.append((
                regiao, item, loc, h["QualityLevel"], h["Timescale"],
                ticks_para_epoch(p["Timestamp"]), p["ItemAmount"],
                p["SilverAmount"], agora))
        self.contagem[regiao][1] += 1

    def _ouro(self, regiao, g):
        tempos = g.get("TimeStamps") or g.get("Timestamps") or []
        for preco, ts in zip(g.get("Prices") or [], tempos):
            self.ouro.append((regiao, ticks_para_epoch(ts), preco))

    # --- gravação ------------------------------------------------------
    def gravar(self):
        ordens, self.ordens = self.ordens, []
        historico, self.historico = self.historico, []
        ouro, self.ouro = self.ouro, []
        contagem = {r: c[:] for r, c in self.contagem.items()}
        for c in self.contagem.values():
            c[0] = c[1] = 0

        cur = self.con.cursor()
        cur.executemany(
            "INSERT INTO ordens VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(regiao, id) DO UPDATE SET preco=excluded.preco, "
            "quantidade=excluded.quantidade, ultima_vez=excluded.ultima_vez",
            ordens)
        cur.executemany(
            "INSERT INTO historico VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO UPDATE SET quantidade=excluded.quantidade, "
            "prata=excluded.prata, recebido=excluded.recebido",
            historico)
        cur.executemany("INSERT OR REPLACE INTO ouro VALUES (?,?,?)", ouro)
        agora = int(time.time())
        for r, (n_ord, n_hist) in contagem.items():
            if n_ord or n_hist:
                cur.execute(
                    "UPDATE status SET ultima_msg=?, msgs_ordens=msgs_ordens+?, "
                    "msgs_historico=msgs_historico+? WHERE regiao=?",
                    (agora, n_ord, n_hist, r))
        self.con.commit()
        return len(ordens), len(historico)

    def limpar(self):
        agora = int(time.time())
        self.con.execute("DELETE FROM ordens WHERE ultima_vez < ?",
                         (agora - RETENCAO_ORDENS_H * 3600,))
        self.con.execute("DELETE FROM historico WHERE ts < ?",
                         (agora - RETENCAO_HISTORICO_D * 86400,))
        self.con.commit()
        log.info("Limpeza de dados antigos concluída")

    # --- laço principal ------------------------------------------------
    async def conectar(self, regiao):
        porta = REGIOES[regiao]["porta"]
        url = f"nats://{NATS_USUARIO}@{NATS_HOST}:{porta}"

        async def erro(e):
            log.warning("[%s] erro NATS: %s", regiao, e)

        async def reconectou():
            log.info("[%s] reconectado", regiao)

        nc = await nats.connect(
            url, name="albion-em-alta", max_reconnect_attempts=-1,
            reconnect_time_wait=5, error_cb=erro, reconnected_cb=reconectou)
        async def receber(msg):
            self.ao_receber(regiao, msg)

        for topico in TOPICOS:
            await nc.subscribe(topico, cb=receber)
        log.info("[%s] conectado em %s:%s", regiao, NATS_HOST, porta)
        return nc

    async def rodar(self):
        conexoes = [await self.conectar(r) for r in self.regioes]
        ultima_limpeza = time.time()
        try:
            while True:
                await asyncio.sleep(INTERVALO_GRAVACAO)
                n_ord, n_hist = self.gravar()
                log.debug("gravadas %d ordens, %d pontos de histórico",
                          n_ord, n_hist)
                if time.time() - ultima_limpeza > INTERVALO_LIMPEZA:
                    self.limpar()
                    ultima_limpeza = time.time()
        finally:
            self.gravar()
            for nc in conexoes:
                await nc.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    regioes = sys.argv[1:] or list(REGIOES)
    invalidas = [r for r in regioes if r not in REGIOES]
    if invalidas:
        sys.exit(f"Regiões inválidas: {invalidas}. Use: {list(REGIOES)}")
    try:
        asyncio.run(Coletor(regioes).rodar())
    except KeyboardInterrupt:
        log.info("Coletor encerrado")


if __name__ == "__main__":
    main()
