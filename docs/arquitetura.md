# Arquitetura

Documento vivo. Toda decisão relevante entra aqui com o **porquê**.

## 1. O problema

O FacebookProspector coletava muitos posts e a maioria era lixo (venda de caminhão, mudança, fora da região, post velho). Eu revisava tudo à mão, e o aprendizado nunca voltava pro sistema.

**Objetivo:** um agente que

1. decide **onde buscar** e **quanto buscar**, dentro de um orçamento;
2. **limpa o lixo** e me entrega só o que aprovou;
3. **aprende** com meu veredito sobre as aprovações dele.

**Meu papel:** revisor do resultado do agente, não triador.

## 2. Visão geral

Duas rotinas e uma ponte com o celular:

| Rotina | Frequência | É agente? | O que faz |
|---|---|---|---|
| **Coleta e triagem** | Diária | Não: pipeline fixo, com LLM só pra julgar posts | Sorteia receitas → busca no Apify → filtra → LLM julga → manda aprovados no Telegram |
| **Respostas** | A cada 2h | Não | Lê meus ✓/✗ no Telegram e grava no banco |
| **Aprendizado** | Semanal (domingo) | **Sim (LangGraph)** | Auditoria → atualiza placares → analisa motivos → propõe e testa ajustes → aplica → resumo |

**Por que só o aprendizado é LangGraph:** a coleta é sempre a mesma sequência; um script resolve. O aprendizado precisa raciocinar sobre meus motivos, usar ferramentas (consultar histórico, testar uma regra), **pausar esperando minhas respostas** e retomar dias depois. É exatamente o que o LangGraph oferece (grafo com estado + interrupt + checkpointer).

## 3. Como o agente escolhe onde buscar

### Receitas de busca

Uma **receita** é um termo de busca + parâmetros do actor do Apify (ex.: `"agregado truck"`, `searchType=latest`). Cada receita tem um placar:

- `alpha` = acertos + 1
- `beta` = erros + 1

### O que conta como acerto e erro

Cada post coletado (após remover duplicados) é uma tentativa da receita que o trouxe:

| Destino do post | Para a receita |
|---|---|
| Agente aprovou **e** eu marquei ✓ | Acerto |
| Agente aprovou e eu marquei ✗ | Erro |
| Agente descartou | Erro (a receita trouxe lixo) |
| Descartado, caiu na auditoria e eu disse "era bom" | Acerto (e o erro é registrado contra a triagem) |
| Aprovado e sem resposta minha em 7 dias | Ignorado |

**Por quê:** medir por post coletado (≈210 por semana), e não só pelos ~10 que eu reviso, dá dados suficientes pro placar convergir. E uma receita que só traz lixo é punida mesmo que eu nunca veja os posts dela.

Post encontrado por mais de uma receita conta para todas.

### O sorteio (Thompson Sampling)

Cada dia, com orçamento de `N` posts:

1. Para cada uma das `N` vagas, sorteia um valor de `Beta(alpha, beta)` de cada receita ativa; a maior leva a vaga.
2. Cada receita busca a quantidade de vagas que ganhou (mínimo 5 se ganhou alguma, pra não rodar o actor por 1 post).

Receitas comprovadas vencem quase sempre (**modo conservador**). Receitas incertas às vezes sorteiam alto e são testadas (**modo exploratório**). Quando tudo começa a errar, as receitas novas passam a vencer mais (**modo agressivo**). Ninguém programa esses modos: eles saem da incerteza.

Referência: [Bernoulli bandits e Thompson Sampling](https://matheusfacure.github.io/2017/03/04/bernoulli-bandits-thompson/).

### Esquecimento

Toda semana, os acertos e erros acumulados são multiplicados por `DECAY = 0.9` (o `+1` do prior não decai). O mercado muda; o que funcionou em março não pode ficar travado como bom pra sempre.

### Ciclo de vida das receitas

- **Ativas:** no máximo 4 ao mesmo tempo.
- **Aposentadoria:** com pelo menos 60 tentativas, se a probabilidade de ela ser melhor que a líder for < 5%.
- **Candidatas:** criadas pelo agente semanal; entra no máximo 1 nova por semana, ocupando a vaga de uma aposentada.
- Toda receita guarda `parent_id` (de qual receita derivou) e `created_by` (`legacy`, `agent`, `manual`).

## 4. Semana sem feedback

O sistema precisa continuar funcionando quando eu não respondo. A regra é: **semana sem feedback = semana congelada.** A coleta continua; o aprendizado pausa.

**Definição:** uma semana é "sem feedback" quando não há nenhuma resposta minha (✓/✗ em post aprovado ou em post de auditoria) entre o domingo anterior e o domingo atual.

### O que muda numa semana sem feedback

| Peça | Comportamento |
|---|---|
| Coleta diária | Continua normal, usando o último estado das receitas e das regras |
| Placar das receitas | **Não é atualizado.** Os descartes da semana ficam registrados no banco, mas não contam como erro |
| Esquecimento (`DECAY`) | **Não é aplicado.** Sem dado novo, esquecer só destruiria conhecimento |
| Aposentadoria de receitas | Não roda |
| Agente semanal | Não chama o LLM nem propõe ajustes. Envia só um resumo: "Sem feedback esta semana, nada mudou." |

**Por quê:** pelas regras de contagem (seção 3), descarte conta como erro e aprovado sem resposta não conta nada. Sem feedback, as receitas só acumulariam erros e todas piorariam artificialmente. E o esquecimento apagaria o passado sem nada novo no lugar.

### Ausências longas

| Semanas seguidas sem feedback | Ação |
|---|---|
| 2 | O bot envia um lembrete no Telegram |
| 4 | `DAILY_RESULT_BUDGET` cai pela metade, para não gastar crédito do Apify sem ninguém olhando |
| Primeira resposta após a ausência | Tudo volta ao normal na semana seguinte; nenhum estado é perdido |

### Implementação

A regra se encaixa no que já existe, sem mudar as funções do bandit:

- **Congelar = pular `atualizar_placares`.** Esse é o único nó que mexe em placar, decay e aposentadoria (seção 6). Em `agent/graph.py`, depois de `aguardar_respostas`, uma aresta condicional vai direto para `enviar_resumo` quando a semana não tem feedback. `decay_all`, `should_retire` e `retire` continuam do jeito que estão.
- **A decisão vive no `core`, com testes.** `core/feedback.py` ganha funções puras que recebem a lista de `Feedback` e dizem se a semana teve resposta e quantas semanas seguidas ficaram sem resposta. O grafo só consulta essas funções.
- **Os descartes da semana congelada não contam depois.** `atualizar_placares` processa só os posts da semana corrente, então a semana pulada não é cobrada na volta.
- **Corte no orçamento:** `core/budget.py`, em `daily_budget`, recebe as semanas seguidas sem feedback e reduz o teto pela metade a partir de 4.
- **Sem tabela nova.** Se a semana teve feedback é derivado das datas dos registros de feedback no banco (etapa 2). O contador de semanas seguidas sai da mesma consulta.

## 5. Triagem (limpar o lixo)

Duas camadas, nessa ordem:

1. **Regras determinísticas** (reaproveitadas do FacebookProspector): duplicados, idade máxima do post, região (raio a partir de Guarulhos), sinais óbvios de não-oportunidade (venda, consórcio, curso).
2. **Juiz LLM:** lê o post e devolve saída estruturada:

```json
{
  "approved": true,
  "score": 0.86,
  "reason": "Procura agregado truck, rota dentro da região, carga seca",
  "vehicle": "truck",
  "origin": "Guarulhos",
  "destination": "Campinas"
}
```

O prompt do juiz é montado a partir de:
- **Regras de triagem** em linguagem natural, versionadas no banco (tabela `triage_rules`). São elas que o agente semanal ajusta.
- **Exemplos reais** (few-shot): meus acertos e erros mais recentes, com motivo.

Os aprovados do dia são ordenados por `score` e enviados ao Telegram. Teto: `MAX_DAILY_NOTIFICATIONS = 3`.

## 6. O agente semanal (LangGraph)

```
preparar_semana
   │  separa 2 descartados aleatórios pra auditoria
   ▼
enviar_auditoria  ──►  Telegram: "Descartei. Confirma?"
   │
   ▼
⏸ aguardar_respostas   (interrupt; retomado pela rotina de respostas
   │                     ou automaticamente na segunda às 8h)
   │
   ├── semana sem feedback? ──►  enviar_resumo  ("nada mudou", seção 4)
   ▼
atualizar_placares     (código puro: alpha/beta, decay, aposentadoria)
   │
   ▼
analisar_motivos       (LLM: agrupa meus motivos de ✗ e os erros da auditoria em padrões)
   │
   ▼
propor_ajustes         (LLM com ferramentas)
   │   tools: consultar_historico, listar_receitas, testar_regra, testar_receita
   ▼
validar_ajustes        (código: guardrails abaixo)
   │
   ▼
aplicar_ajustes  ──►  enviar_resumo  ──►  Telegram: resumo + botão [Desfazer ajustes]
```

### Ferramentas do agente

| Ferramenta | O que faz |
|---|---|
| `consultar_historico` | Busca posts rotulados por filtro (período, receita, motivo) |
| `listar_receitas` | Receitas com placar, taxa estimada e status |
| `testar_regra` | Roda uma regra de triagem proposta contra o histórico rotulado e devolve quantos acertos/erros ela teria causado (**backtest**) |
| `testar_receita` | Busca de teste no Apify com 5 resultados, para ver se o termo traz algo (gasta orçamento; máx. 2 por semana) |

### Guardrails (código, não LLM)

O LLM **propõe**; o código **decide** se aplica:

- Regra de triagem nova só entra se o backtest não piorar a taxa de acerto nem descartar mais de 1 post que eu marquei como bom.
- No máximo 1 receita nova e 3 mudanças de regra por semana.
- O LLM não mexe em placar, orçamento nem em receitas fora do ciclo de vida.
- Tudo que é aplicado vai pro `changelog` com o motivo; o botão **Desfazer ajustes** reverte a semana inteira.

**Por que ajustes automáticos:** eu não aprovo antes; avalio o efeito na semana seguinte. O botão de desfazer é a rede de segurança.

## 7. Orçamento

Apify, actor `scraper_one/facebook-posts-search`, plano Free: US$ 5/mês de crédito ≈ 1.000 posts. O crédito não acumula e, quando acaba, o Apify bloqueia (não cobra).

| Parâmetro | Valor |
|---|---|
| `MONTHLY_RESULT_BUDGET` | 900 posts |
| `DAILY_RESULT_BUDGET` | `(orçamento restante no mês) / (dias restantes)`, teto 40 |
| Testes do agente (`testar_receita`) | Saem do mesmo orçamento |

O orçamento é uma **trava em código**. O agente decide como distribuir, nunca quanto gastar acima do teto.

## 8. Telegram

**Mensagem de post aprovado:**

```
Novo post aprovado
"Procuro agregado com truck pra rota Guarulhos–Campinas..."
Local: Guarulhos · há 3h · receita: agregado truck
Por que aprovei: truck, dentro da região, carga seca
[Abrir post]
[✓ Certo] [✗ Fora da região] [✗ Não é oportunidade] [✗ Outro]
```

- Os botões de ✗ mostram os 3 motivos mais frequentes das últimas semanas + "Outro".
- Para "Outro", eu respondo (reply) à mensagem com o motivo em texto.
- **Recebimento:** sem servidor. A rotina de respostas usa `getUpdates` a cada 2h (o Telegram guarda atualizações por até 24h).
- Melhoria futura: webhook num Cloudflare Worker, pra resposta instantânea.

## 9. Arquitetura de código (arquitetura limpa)

```
src/prospector/
├── core/            regras de negócio puras, SEM dependências externas
│   ├── post.py          Post, TriageResult
│   ├── recipe.py        Recipe (termo, params, alpha, beta, status)
│   ├── feedback.py      Feedback (post, veredito, motivo, origem)
│   ├── bandit.py        Thompson Sampling, decay, aposentadoria
│   └── budget.py        cálculo do orçamento diário
├── flows/           casos de uso: o que o sistema faz
│   ├── ports.py         interfaces: SearchProvider, Repository, Judge, Messenger
│   ├── collect.py       coleta e triagem diária
│   ├── ingest_replies.py
│   └── import_legacy.py
├── agent/           o agente semanal
│   ├── graph.py
│   ├── state.py
│   ├── nodes.py
│   └── tools.py         tools chamam os flows, nunca as integrações direto
└── integrations/    implementações concretas das portas
    ├── apify.py
    ├── telegram.py
    ├── postgres.py
    └── llm.py           Claude via langchain-anthropic
```

**Regra de dependência:** `integrations → flows → core`. O `core` não importa nada de fora. Por isso o bandit e o orçamento são testáveis em milissegundos, sem internet.

**Modelos:** um modelo pequeno e barato para o juiz diário; um modelo maior para o agente semanal.

## 10. Infraestrutura

| Peça | Escolha | Por quê |
|---|---|---|
| Execução | GitHub Actions | Gratuito e ilimitado em repositório público |
| Banco | PostgreSQL no Supabase (free) | Actions não tem disco permanente; LangGraph tem `PostgresSaver` pronto |
| Segredos | GitHub Secrets | Nunca no código |

**Workflows:**

| Arquivo | Quando (BRT) | Faz |
|---|---|---|
| `collect.yml` | Todo dia, 7h | Coleta e triagem |
| `replies.yml` | A cada 2h | Ingere respostas; retoma o grafo semanal se a auditoria foi respondida |
| `weekly.yml` | Domingo, 10h | Inicia o agente semanal |
| `ci.yml` | Todo PR | Lint + testes |

Cron do Actions é em UTC (BRT + 3h) e pode atrasar alguns minutos.

## 11. Histórico do FacebookProspector

O banco antigo (`prospector.db`, SQLite) tem o que precisamos:

| Tabela/campo antigo | Uso aqui |
|---|---|
| `posts.search_query` | Liga cada post à receita de origem |
| `posts.user_action` = `CHAMEI` | Acerto |
| `posts.user_action` = `NAO_CHAMEI` | Erro |
| `posts.processing_status` = `FILTERED` | Erro (lixo) |
| `user_actions.reason` | Motivo → exemplos few-shot e regras iniciais |
| `post_search_matches` | Posts que apareceram em mais de uma busca |

**Importação (`flows/import_legacy.py`):**

- Cada `search_query` antigo vira uma receita com prior `Beta(1 + k·acertos, 1 + k·erros)`, com `k = 0.2` (dado velho vale menos).
- Posts rotulados viram base de exemplos e de backtest.
- O agente roda uma vez `analisar_motivos` sobre o histórico para gerar as **regras de triagem iniciais**.
- Campos de autor (nome, perfil) não são importados: não são necessários para aprender.

## 12. Fora do escopo (por enquanto)

- RAG
- Coletor de grupos (`apify/facebook-groups-scraper`)
- Interface web
- Multiusuário
