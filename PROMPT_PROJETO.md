# Prompt de contexto — Projeto "Albion em Alta"

> Cole este texto no início de uma conversa com uma IA (ou entregue a um desenvolvedor) para que ela entenda o projeto inteiro antes de mexer nele.

---

Você vai trabalhar no projeto **Albion em Alta**, um site público em **português do Brasil** que mostra o que está em alta no mercado do jogo **Albion Online**, com dados abertos do **Albion Data Project** (AODP). Repositório: https://github.com/LorranBRS/Projeto-ALBION. Responda sempre em pt-BR.

## 1. Objetivo

Mostrar ao jogador, quase em tempo real:
- quais itens estão vendendo acima do normal, subindo ou caindo de preço;
- onde comprar e onde vender (comparação entre cidades e Mercado Negro);
- uma previsão de quais itens tendem a subir nas próximas 12h;
- quanto custa fabricar/refinar um item, onde fabricar e se dá lucro;
- o bônus de produção do dia;
- ciclos semanais de preço dos insumos (fundo para comprar, pico para vender).

## 2. Arquitetura

```
NATS do AODP (tempo real) ──► coletor.py ──► dados/mercado.db (SQLite, WAL) ──► servidor.py (FastAPI) ──► site/index.html
API REST do AODP ──────────► carga_inicial.py ──┘                                   ▲
ao-bin-dumps (GitHub) ─────► itens.py / receitas.py (caches JSON) ──────────────────┤
previsao.py (1x/dia) ──────► dados/modelo.json ─────────────────────────────────────┤
ciclos.py   (1x/dia) ──────► dados/ciclos.json ─────────────────────────────────────┘
```

Stack: Python 3.12, `nats-py`, `fastapi` + `uvicorn`, `httpx`, `numpy`, `pandas`, `statsmodels`, `lightgbm`, `scikit-learn`, SQLite. Frontend: um único HTML com JS puro (sem framework), tema claro/escuro, responsivo.

## 3. Arquivos

| Arquivo | Papel |
|---|---|
| `config.py` | Caminhos, regiões (west 4222 / europe 34222 / east 24222), tópicos NATS, escalas do histórico, retenção (ordens 48h, histórico 180 dias), conversão de ticks C# → epoch. |
| `coletor.py` | Processo 24h. Assina `marketorders.deduped`, `markethistories.deduped`, `goldprices.deduped` nas 3 regiões; grava em lote a cada 5 s; limpa dados antigos a cada 1h. Tabelas: `ordens`, `historico`, `ouro`, `status`. |
| `carga_inicial.py` | Preenche o histórico de ~12 mil itens pela API REST (`/api/v2/stats/history`, `time-scale=1` e `6`), 100 itens por requisição, **gzip obrigatório**, pausa de 3 s, trata 429. ~12 min por região. |
| `itens.py` | Cache `dados/itens_cache.json` a partir do `formatted/items.json` e `world.json` do ao-bin-dumps: código → nome PT-BR, tier, encantamento, índice numérico; locais; portais somados à cidade. |
| `receitas.py` | Cache `dados/receitas_cache.json` a partir dos arquivos **brutos** do ao-bin-dumps (`items.json`, `craftingmodifiers.json`, `festivities.json`): ~9.300 receitas, valor dos itens, bônus por cidade, lista de bônus diários. |
| `limpeza.py` | Remoção de picos de preço (manipulação) e detecção de preço parado. |
| `previsao.py` | Treina e valida o modelo de tendência de alta; grava `dados/modelo.json`. |
| `ciclos.py` | Análise de ciclos semanais dos insumos (STL + EMA + Holt-Winters), paralelizada; grava `dados/ciclos.json`. |
| `servidor.py` | API JSON + página. Cache em memória de 60 s (previsão 10 min, bônus 30 min). Pré-calcula a previsão ao iniciar. |
| `site/index.html` | Interface completa. |

## 4. Fatos sobre os dados (aprendidos na prática — não "corrigir")

- Preços do NATS e da REST já vêm em **prata**, sem multiplicador.
- `LocationId`: 7 Thetford, 1002 Lymhurst, 2004 Bridgewatch, 3005 Caerleon, 3008 Martlock, 4002 Fort Sterling, 5003 Brecilien, **3003 Mercado Negro**. Portais (301, 1301, 2301, 3301, 4301) contam como a cidade.
- No histórico do NATS o item vem como número (`AlbionId`) = campo `Index` do `items.json`.
- `Timescale`: 0 = blocos de 1h (24h), 1 = blocos de 6h (7 dias), 2 = blocos de 6h (28 dias).
- Ticks C#: `epoch = (ticks − 621355968000000000) / 10⁷`.
- O histórico de um item só chega pelo NATS quando algum jogador abre a aba de histórico dele no jogo → por isso existe a carga inicial pela REST.
- A REST bloqueia em segundos sem `Accept-Encoding: gzip`. A REST só guarda ~30 dias.
- Itens encantados no mercado: `T8_METALBAR_LEVEL1@1`, `T8_MAIN_SWORD@2`.
- Bônus do dia: `GET https://{west|europe|east}.albion-online-data.com/api/v2/stats/festivities`, eventos `Kind 2` (ex.: `COMMON_MACE`) mapeados por `festivities.json` → categoria e valor (+10% ou +20%). Retorna 404 quando não há snapshot confirmado.
- Dumps mensais (~260–300 MB cada, 2025 → hoje) em `albion-online-data.com/database/` — ainda **não** importados.

## 5. Estratégias e algoritmos

### 5.1 Limpeza de picos (manipulação de preço)
Cada ponto do histórico (preço médio do bloco) é comparado com a **mediana** dos pontos do mesmo item + cidade + qualidade (referência: escala de 6h). Fora de **3× acima ou abaixo** (`FATOR_PICO`) → descartado. ~1,4% dos pontos caem nisso. Aplicado em ranking, detalhe, comparação, previsão e ciclos. Itens cujo preço não varia mais de 1% em 7 dias saem do ranking (`preco_parado`).

### 5.2 Rankings (máx. 20 itens por aba)
- **Em alta**: `crescimento = vendas 24h ÷ média diária dos 7 dias anteriores` (só combinações item/cidade/qualidade com dados nas duas escalas). Índice = `crescimento × √(prata 24h) / 100` quando crescimento > 1.
- **Mais vendidos**: prata movimentada em 24h.
- **Preço subindo/caindo**: preço médio 24h vs 7 dias anteriores (mín. 20 vendas).
- **Movimento agora**: ordens novas na última hora ÷ média por hora das 24h anteriores (precisa de 2h de coleta).
- **Mercado Negro**: top 5 do índice "em alta" no local 3003.
- Filtros: busca no catálogo inteiro (sem acento), cidade, tier T1–T8 e encantamento .0–.4 (seleção múltipla), "última venda" (1h–24h, padrão 12h).

### 5.3 Comparar mercados
Preço médio de 3 dias (qualidade Normal, sem picos, referência de 10 dias) do mesmo item em A e B. `lucro = preço_B × (1 − 6,5%) − preço_A` (4% imposto premium + 2,5% anúncio). Mín. 5 vendas/dia em cada. Ordenação: lucro por unidade, %, ou lucro × vendas/dia.

### 5.4 Previsão de tendência (topo da página)
- Regressão linear **ridge** sobre blocos de 6h (escala 2, qualidade Normal, sem picos). Horizonte: preço médio das **próximas 12h vs últimas 24h**.
- Indicadores: `momento_curto` (último bloco vs 3 anteriores), `momento_24h` (24h vs 6 dias anteriores), `volume` (vendas 24h vs média diária), `desvio` (preço 24h vs mediana 7 dias). Entradas recortadas em ±2, alvo em ±1.
- Padrão aprendido: **reversão à média** (pesos ≈ momento_curto +0,023, momento_24h −0,022, volume −0,008, desvio −0,038).
- Elegível ao top: ≥ 50 vendas em 24h e |desvio| ≤ 0,35.
- Validação (últimos 7 dias fora do treino): acerto de direção 58% (base: 47% sobem), correlação 0,41, **top 3 subiu em 64%**, alta típica +12,9%.
- Exibe **3 itens gerais + 1 destaque do Mercado Negro** (mesmo modelo, filtrado em 3003), com motivos derivados das contribuições dos pesos.

### 5.5 Fabricação e refino (aba "Fabricação" no detalhe do item)
- Receita do `items.json` bruto (primeira opção; encantamentos vêm do bloco `enchantments`). Artefatos/tokens têm `maxreturnamount=0` (não retornam).
- `valor do item = Σ qtd × itemvalue dos recursos` → `nutrição = valor × 0,1125` → `taxa da estação = nutrição × taxa / 100` (taxa digitada pelo usuário).
- Bônus de produção da cidade: base 18% (refino nas cidades reais 18%, nos Descansos 15%) + especialidade (+15% fabricação da categoria, +40% refino do recurso) + bônus do dia + 59% com foco.
- `taxa de retorno = 1 − 1/(1 + bônus)` → 15,2% / 24,8% / 36,7% / 47,9% / 53,9% (bate com o jogo).
- Custo efetivo = materiais retornáveis × (1 − retorno) + não retornáveis + taxa. Lucro = venda × (1 − 6,5%) − custo.
- Preço dos materiais: média de 3 dias, qualidade Normal, cidade mais barata. Preço de venda do item final: todas as qualidades, 14 dias + ordens de 48h (itens caros vendem pouco).
- Refino dos materiais: receita (ex.: 5 minérios + 1 barra do tier anterior), melhor cidade de refino e "compensa refinar ou comprar pronto".
- Cidades: Thetford (minério, maças, cajado natureza/fogo, armadura couro, capuz tecido), Lymhurst (fibra, espadas, arcos, arcano, capuz/sapato couro), Bridgewatch (pedra, bestas, adagas, amaldiçoado, armadura placa, sandália tecido), Martlock (pele, machados, bordões, gelo, bota placa, mão secundária), Fort Sterling (madeira, martelos, lanças, sagrado, elmo placa, robe tecido), Caerleon (coleta, ferramentas, comida, manoplas, metamorfo), Brecilien (capas, bolsas, poções), Descansos de Artur/Merlin/Morgana (placa/couro/tecido).

### 5.6 Ciclos semanais dos insumos (aba "Ciclos semanais")
- Grupos: recursos brutos, refinados, comida, poções, peixes, fazenda/ervas, montarias (por `shopcategory/shopsubcategory1`).
- Série por item + cidade (6h, ≥ 3 semanas, ≥ 70% de cobertura, ≥ 20 vendas/dia), lacunas interpoladas, log do preço.
- **STL** robusta (período 28 blocos = 7 dias) → curva semanal (% vs normal por faixa de 6h) e **força sazonal** (Hyndman). **EMA de 3 dias** = nível atual. **Holt-Winters** (tendência amortecida + sazonalidade aditiva) = previsão de 7 dias.
- Fundo (comprar) = menor faixa; pico (vender) = maior faixa depois do fundo; margem líquida com 6,5% de taxas.
- Validação na última semana: **o ciclo não se confirmou** com 4 semanas de dados (estratégia fundo→pico lucrou em ~16% das séries; mesmo sem taxa ~42%; Holt-Winters errou 7,2% vs 5,6% de "repetir o último preço"). Por isso só recebem sinal comprar/vender os itens cujo ciclo se confirmou no teste e cuja previsão bateu o último preço; o site mostra um aviso.
- Roda com `multiprocessing` e **`OMP_NUM_THREADS=1`** (sem isso o BLAS satura a CPU). ~2 min.

### 5.7 Módulo preditivo v2 (pasta `backend/`)
- `backend/analytics/features.py` → `build_market_features(df)`: spread_relative (ordens atuais), volume_rolling_7d/30d, price_std_7d, z_score_14d, sin/cos de dia da semana e hora, lag_6h/12h/24h/7d, pct_change_*, cost_theoretical e crafting_margin_ratio. Base: preço médio negociado em blocos de 6h (não existe histórico de sell_price_min).
- `backend/models/predictor.py`: LightGBM com perda MAE, alvo = log(preço +12h ou +24h / preço atual); intervalos 80%/95% por regressão quantílica + ajuste conformal (CQR); validação `TimeSeriesSplit` walk-forward com gap = horizonte. Resultado: erro 16% menor que "não muda", direção 66%, cobertura 79,7% (80%) e 94% (95%).
- Horizontes 12h, 24h, 3d e 7d (7d: erro 10% menor que "não muda", direção 62%, cobertura 75%/92%). Aba "Previsão 7 dias / 4 semanas" com filtro de previsões firmes (faixa ≤ 40% do preço e 20+ vendas/dia). 4 semanas = projeção não validada (volatilidade semanal, desvio_k = √(desvio_sem1² + vol² × (k−1))) até haver 56+ dias de histórico.
- `backend/services/signals.py` → `generate_trade_signals`: COMPRA se preço < limite inferior − 6,5% e z < −1,5; VENDA se preço > limite superior − 6,5% e z > 1,5; senão NEUTRO. Teste: venda acertou 77%; compra 93%, mas aparece em quedas bruscas (conferir no jogo).
- `backend/api/routes.py`: rotas `/api/v1/...`; sinais de todos os itens em lote (cache 30 min, trava por região).
- Interface: selo verde/vermelho/cinza nos itens, aba "Previsão" no detalhe (gráfico com faixas de confiança, z-score em texto), aba "Oportunidades de fabricação" (margem > 1,15×, ordenada pelo insumo com menor z-score).

## 6. API (servidor.py)

`/api/regioes` · `/api/cidades` · `/api/em-alta?regiao=&cidade=` · `/api/busca?q=` · `/api/item/{regiao}/{item}` · `/api/comparar?regiao=&a=&b=` · `/api/previsao?regiao=` · `/api/bonus-do-dia?regiao=` · `/api/craft/{regiao}/{item}` · `/api/ciclos?regiao=` · `/api/ciclo/{regiao}/{item}` · `/` (página).
v2: `/api/v1/predictions/{item_id}?regiao=&cidade=` · `/api/v1/signals?regiao=` · `/api/v1/crafting/opportunities?regiao=&limite=` · `/api/v1/model`.

## 7. Interface (site/index.html)

Cabeçalho com servidor, botão Atualizar e tema claro/escuro (salvo no navegador) → faixa **Bônus do dia** → **Previsão** (4 cartões centralizados; o 4º, roxo, é do Mercado Negro, com o acerto do teste) → abas: Em alta, Mais vendidos, Preço subindo, Preço caindo, Movimento agora, Comparar mercados, Ciclos semanais, Mercado Negro → filtros → tabela. Clicar num item abre o detalhe com abas **Mercado** (gráficos 24h e 28 dias, preços por cidade), **Fabricação** e **Ciclo semanal**. Preferências (região, cidade, filtros, taxa da estação, foco, tema) ficam no `localStorage`.

## 8. Como rodar

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python itens.py && .venv/bin/python receitas.py
.venv/bin/python coletor.py                     # deixar ligado 24h
.venv/bin/python carga_inicial.py west          # uma vez (ou a cada poucas horas)
.venv/bin/python previsao.py                    # 1x por dia
OMP_NUM_THREADS=1 .venv/bin/python ciclos.py    # 1x por dia
.venv/bin/python -m backend.models.predictor    # 1x por dia (LightGBM v2)
.venv/bin/uvicorn servidor:app --port 8000
```

## 9. Limitações conhecidas e próximos passos

- Ainda **sem hospedagem pública**: precisa de servidor 24h (coletor). Opções: túnel do PC (Cloudflare) ou VPS.
- Retreino diário de `previsao.py` e `ciclos.py` não está automatizado (cron/systemd).
- Importar os dumps mensais do AODP para validar ciclos semanais com 1 ano de dados.
- Europa e Ásia têm bem menos dados que Américas.
- Descansos de Artur/Merlin/Morgana não têm preço de venda (mercado não acompanhado).
- Taxas de venda (6,5%), fator de nutrição (0,1125) e foco (+59%) vêm do conhecimento da comunidade, não de arquivos do jogo.

## 10. Regras para quem for mexer

- Textos da interface e comentários em pt-BR.
- Nunca chamar a REST do AODP sem gzip e sem pausa.
- Toda métrica nova de preço deve passar pela limpeza de picos.
- Toda "previsão" ou "estratégia" nova deve ter validação fora da amostra e mostrar o acerto no site — não exibir promessa sem teste.
- Mantenha o limite de 20 itens por ranking.
