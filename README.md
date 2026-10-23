# Albion em Alta

Site público que mostra o que está em alta no mercado do **Albion Online**, com
dados abertos do [Albion Data Project](https://www.albion-online-data.com/).

## Como funciona

```
NATS (tempo real) ──► coletor.py ──► dados/mercado.db (SQLite) ──► servidor.py ──► site/index.html
API REST ──────────► carga_inicial.py ──┘
```

| Arquivo | Função |
|---|---|
| `coletor.py` | Fica ligado 24h. Assina `marketorders`, `markethistories` e `goldprices` (tópicos `.deduped`) das 3 regiões e grava no SQLite. Apaga ordens com mais de 48h e histórico com mais de 30 dias. |
| `carga_inicial.py` | Preenche o histórico de todos os itens pela API REST (24h por hora + 28 dias em blocos de 6h). Rode ao instalar e, se quiser, a cada poucas horas. |
| `servidor.py` | API JSON (`/api/em-alta`, `/api/busca`, `/api/item/{regiao}/{item}`, `/api/regioes`, `/api/cidades`) e a página. Cache de 60 s por consulta. |
| `previsao.py` | Treina o modelo de previsão (regressão sobre blocos de 6h dos últimos 28 dias), mede o acerto nos últimos 7 dias fora do treino e grava `dados/modelo.json`. Rode 1x por dia. |
| `limpeza.py` | Descarta picos de preço fora do padrão (mais de 3x acima ou abaixo da mediana do item/cidade/qualidade) e marca itens com preço parado. Usado no ranking, detalhe, comparação e previsão. |
| `receitas.py` | Receitas de fabricação e refino, valor dos itens, bônus das cidades e lista de bônus diários, a partir dos arquivos brutos do `ao-bin-dumps` (`items.json`, `craftingmodifiers.json`, `festivities.json`). Gera `dados/receitas_cache.json`. Rode após um patch do jogo. |
| `ciclos.py` | Ciclos semanais dos insumos (recursos, refinados, comida, poções, peixes, fazenda/ervas, montarias): curva semanal por STL, nível por EMA de 3 dias, previsão de 7 dias por Holt-Winters e validação na última semana. Gera `dados/ciclos.json`. Rode 1x por dia (~2 min, usa todos os núcleos). |
| `itens.py` | Tabela de itens (nomes em PT-BR) e locais, a partir do `ao-bin-dumps`. `python itens.py` atualiza após um patch do jogo. |
| `site/index.html` | Página única: abas de ranking, filtros, detalhe do item com gráficos. |

## Rankings

- **Em alta**: vendas das últimas 24h ÷ média diária dos 7 dias anteriores, pesado pelo volume em prata.
- **Mais vendidos**: prata movimentada em 24h.
- **Preço subindo / caindo**: preço médio de 24h comparado ao dos 7 dias anteriores.
- **Movimento agora**: ordens novas na última hora comparadas ao ritmo normal por hora (precisa de 2h de coleta).
- **Mercado Negro**: os 5 itens mais em alta no Mercado Negro de Caerleon (local 3003), em cartões.

- **Comparar mercados**: escolha onde comprar e onde vender; mostra o preço médio de 3 dias (qualidade Normal) nos dois e o lucro por unidade já descontando 6,5% de taxas (4% de imposto com premium + 2,5% de anúncio).
- **Previsão (topo da página)**: os 3 itens com maior alta prevista para as próximas 12h, com o motivo. Só entram itens com 50+ vendas em 24h e preço sem anomalia. O acerto exibido vem do teste fora do treino.

Cada ranking mostra no máximo 20 itens. Filtros:
- **Tier** (T1–T8) e **encantamento** (.0–.4) com seleção múltipla; nenhum marcado = todos.
- **Última venda**: esconde itens sem venda recente (padrão: 12h). A busca ignora esse filtro.
- **Picos de preço**: pontos de histórico mais de 3x acima ou abaixo da mediana são ignorados em todos os cálculos.
- **Preço parado**: itens cujo preço não variou mais de 1% em 7 dias saem do ranking.

 A busca procura no catálogo inteiro (`/api/busca`),
inclusive itens sem vendas recentes. Filtros: tier, encantamento e cidade (`/api/em-alta?cidade=<id>`).
O botão de tema alterna entre claro e escuro, e a escolha fica salva no navegador.

## Rodar localmente

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python itens.py
.venv/bin/python coletor.py            # deixe rodando em um terminal
.venv/bin/python carga_inicial.py      # em outro terminal, uma vez
.venv/bin/python previsao.py          # treina o modelo (depois da carga inicial)
.venv/bin/uvicorn servidor:app --port 8000
```

Abra http://localhost:8000.

## Fabricação (aba no detalhe do item)

- Materiais da receita com o preço mais barato e a cidade (preço médio de 3 dias, sem picos).
- Valor do item, nutrição gasta (`valor × 0,1125`) e taxa da estação (`nutrição × taxa / 100`, a taxa é informada pelo usuário).
- Para cada cidade: bônus de produção (base 18% + especialidade da cidade + bônus do dia + 59% com foco),
  taxa de retorno (`1 − 1/(1 + bônus)`), custo efetivo, preço de venda e lucro após 6,5% de taxas.
- Refino dos materiais: cidade com bônus de refino (+40%), receita (ex.: 5 minérios + 1 barra do tier anterior) e se compensa refinar ou comprar pronto.
- **Bônus do dia** (topo da página): categorias sorteadas com +10%/+20% de produção, lidas de `/api/v2/stats/festivities` do Albion Data Project (cache de 30 min).

## Ciclos semanais (aba "Ciclos semanais" e aba no detalhe do item)

- Curva semanal de cada insumo por cidade e de cada grupo: % acima/abaixo do normal em cada faixa de 6h.
- Próxima janela de compra (fundo) e de venda (pico), margem histórica já descontando 6,5% de taxas, força do ciclo.
- Validação fora da amostra exibida no site. Na primeira rodada (4 semanas de dados, out/2026) o ciclo **não**
  se confirmou: a estratégia fundo→pico lucrou em ~16% das séries e o Holt-Winters errou mais (7,2%) que repetir o
  último preço (5,6%). Por isso, só recebem sinal de comprar/vender os itens cujo ciclo se confirmou na semana de teste.
- Com mais semanas de histórico (retenção agora de 180 dias, ou importando os dumps mensais do Albion Data Project)
  a validação fica mais confiável.

## Detalhes dos dados (aprendidos no projeto)

- Preços do NATS e da API já vêm em prata (sem multiplicador).
- `LocationId` 3003 é o Mercado Negro; portais (301, 1301, …) são somados à cidade.
- O histórico do NATS identifica o item pelo número (`AlbionId`) = campo `Index` do `items.json`.
- `Timescale`: 0 = por hora (24h), 1 = blocos de 6h (7 dias), 2 = blocos de 6h (28 dias).
- Datas em ticks do C#: `epoch = (ticks - 621355968000000000) / 10000000`.
- A API REST bloqueia rápido quem faz muitas requisições sem compressão. Use `Accept-Encoding: gzip` e pausas.
