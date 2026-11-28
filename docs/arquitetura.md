# Arquitetura

Documento vivo. Toda decisão relevante entra aqui com o **porquê**.

## 1. O problema

O FacebookProspector coletava muitos posts e a maioria era lixo (venda de caminhão, mudança, fora da região, post velho). Eu revisava tudo à mão, e o aprendizado nunca voltava pro sistema.

**Objetivo:** um agente que

1. decide **onde buscar** e **quanto buscar**, dentro de um orçamento;
2. **limpa o lixo** e me entrega só o que aprovou;
3. **aprende** com meu veredito sobre as aprovações dele.

**Meu papel:** revisor do resultado do agente, não triador.

### A operação (o que define "oportunidade")

Uma transportadora com caminhões **truck e toco, com baú**, baseada em **Guarulhos (SP)**. O que ela procura é empresa que queira agregar esses caminhões, ou que tenha carga para eles.

**A limitação que manda em tudo: o caminhão sai da base e volta para a base.** Não compensa deslocar o caminhão para operar em outra cidade, mesmo perto. Por isso:

- **a origem** (onde o caminhão carrega ou se apresenta) precisa ficar a **até ~30 km de Guarulhos**: Guarulhos, Arujá, Itaquaquecetuba, Poá, Mairiporã, as zonas Norte e Leste de São Paulo; em linha reta, isso inclui a capital, o ABC e Osasco. Uma operação baseada em Sorocaba, Campinas, Sumaré, Jundiaí ou Mogi não serve, mesmo a menos de 100 km;
- **o destino** pode ser longe, **desde que dentro do estado de SP**: sair de Guarulhos e entregar em Campinas, voltando, serve;
- **fora do estado de SP** não serve.

Esse contexto vai nas regras fixas (seção 5, região), nas regras do juiz (`triage_rules`) e no papel do juiz (`integrations/llm.py`).

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

- **Congelar = pular `atualizar_placares`.** Esse é o único nó que mexe em placar, decay e aposentadoria (seção 6). Em `agent/graph.py`, depois de `aguardar_respostas`, uma aresta condicional vai para `congelar_semana` e depois `enviar_resumo` quando a semana não tem feedback. Nenhum LLM é chamado.
- **A decisão vive no `core`, com testes.** `core/feedback.py` tem `week_had_feedback` (a semana teve resposta?) e `silent_weeks` (quantas semanas seguidas sem resposta). O grafo e a coleta só consultam essas funções.
- **O silêncio conta a partir da primeira pergunta.** `silent_weeks` mede desde o mais recente entre a minha última resposta e a primeira mensagem que o sistema me mandou. Sem isso, o histórico importado (respostas de setembro e outubro) faria o sistema começar achando que eu sumi há meses e cortaria o orçamento no primeiro dia.
- **Os descartes da semana congelada não contam depois.** Cada post entra no placar uma vez só, registrado em `scored_posts`. `congelar_semana` marca os posts da semana como `frozen`, e eles saem da fila sem contar. O aprovado ainda no prazo de 7 dias continua na fila: uma resposta atrasada ainda vale.
- **Corte no orçamento:** `daily_budget` (`core/budget.py`) recebe as semanas sem resposta e corta o teto pela metade a partir de 4. A coleta calcula isso a cada dia; a primeira resposta desfaz o corte sozinha.

## 5. Triagem (limpar o lixo)

Duas camadas, nessa ordem:

1. **Regras determinísticas** (`core/rules.py`, portadas do FacebookProspector). Rodam nesta ordem, e o motivo mais específico ganha:
   1. idade: post com mais de `MAX_POST_AGE_HOURS = 48`;
   2. veículo ou carroceria incompatível (carreta, VUC, fiorino, sider, graneleiro…), a não ser que a oferta também cite o que serve (oferta mista passa, marcada);
   3. sem texto **e** sem imagem;
   4. idioma estrangeiro ("agregado" em espanhol é "adicionado");
   5. sem sinal de frete, só quando há texto e **não** há imagem;
   6. venda, financiamento, curso, oficina, proteção veicular, plataforma de cotação (com exceção para quem também fala de agregamento, menos anúncio de veículo);
   7. região (`core/region.py`): outro estado, ou **nenhuma** cidade citada a até 30 km de `HOME_BASE` (seção 1: a origem precisa ser perto; o destino pode ser longe). "Guarulhos x Campinas" passa, porque Guarulhos está no raio; "operação em Sorocaba" cai. Sem cidade reconhecida, segue. É uma aproximação: a regra não sabe qual cidade é a origem. Um post que só cita o destino ("entrega em Campinas") cai, e isso é aceito porque a origem desconhecida raramente é Guarulhos.

   "Motorista se oferecendo" não é filtro: vira um sinal para o juiz. Duplicados são resolvidos antes das regras, pelo id do post.
2. **Juiz LLM:** lê o post (texto **e imagem**) e devolve saída estruturada:

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

### Posts só de imagem

Muita oportunidade é postada só como imagem: um flyer, um print com o WhatsApp. No histórico antigo, 6 dos 19 posts que eu chamei eram assim. Então:

- `Post.text` pode vir vazio quando há `image_url`. Post sem texto **e** sem imagem é descartado; sem texto, não.
- O Apify traz, em cada imagem, o texto alternativo que o Facebook gera (`accessibilityCaption`: "May be an image of text that says '…'"). A coleta junta ao texto do post **só a transcrição entre aspas**. A descrição genérica fica de fora: é inglês e confundiria a regra de idioma.
- As regras determinísticas que dependem de texto (região, palavras de venda) não descartam um post só porque não acharam texto. Na dúvida, quem decide é o juiz.
- O juiz recebe a imagem junto do texto (o modelo é multimodal).
- A URL de imagem do Facebook expira em poucos dias. O juiz julga no dia da coleta, com a URL original. Para o backtest e o agente semanal reverem o post depois, **a coleta guarda uma cópia da imagem** no Supabase Storage:
  - bucket `post-images`, **privado** (as imagens têm nome e telefone); o caminho fica em `posts.image_path` (`AAAA/MM/<id>.<ext>`, separado por mês para dar para apagar o que é velho);
  - só dos posts que foram ao juiz: os barrados pelas regras são lixo claro, e o plano grátis tem 1 GB. Com ~30 posts por dia e uns 200 KB por imagem, são poucos MB por mês;
  - imagem que não baixa, não é imagem ou passa de 5 MB fica sem cópia, e a coleta segue;
  - o backtest troca a URL expirada por um link assinado da cópia (vale 1 h). Post só de imagem **com** cópia passa a ser medido; sem cópia continua pulado;
  - chave secreta (`sb_secret_...`) só no `.env` e nos Secrets. `prospector storage check` confere chave e bucket e testa gravar, ler e apagar.

### Prompt do juiz

O prompt do juiz (`integrations/llm.py`) é montado a partir de:
- **Regras de triagem** em linguagem natural, versionadas no banco (tabela `triage_rules`). São elas que o agente semanal ajusta. As 7 regras iniciais (`created_by = seed`) foram escritas a partir dos motivos das minhas 124 respostas no FacebookProspector.
- **Exemplos reais** (few-shot): 4 "chamei" e 8 "não chamei" mais recentes. Equilibrados, porque no histórico há 105 "não chamei" para 19 "chamei": na proporção real, o juiz aprenderia que quase tudo é ruim. Nos "chamei" sem motivo, o motivo é "eu chamei".
- **Sinais das regras fixas**: veículo, carroceria, região, oferta mista e "parece motorista se oferecendo".

**Motorista se oferecendo é reprovação.** O antigo só marcava; minhas respostas mostram que eu nunca chamei ("procurando trampo", "quer trampo"). O interesse é em quem tem carga.

**Como o juiz é chamado:** SDK oficial da Anthropic, `messages.parse` com o formato da resposta definido em Pydantic (`approved`, `score`, `reason`, `vehicle`, `origin`, `destination`). É uma chamada só, sem ferramentas: não precisa de LangChain. O `system` (papel, regras, exemplos) é estável e vai com `cache_control`; do segundo post do dia em diante, ele é lido do cache a um décimo do preço. A imagem vai por URL, antes do texto; se ela não carregar, o juiz tenta de novo só com o texto. Recusa ou erro da API viram reprovação com o motivo "juiz indisponível", e a coleta segue.

**Backtest** (`prospector backtest`): regras e juiz contra as minhas respostas, com o gabarito "eu teria chamado?". Mede três recortes: as regras sozinhas, o juiz nos posts que passaram nas regras, e as duas camadas juntas. Os posts usados como exemplo ficam fora da medição. Posts só de imagem do histórico são pulados, porque as URLs antigas expiraram.

Os aprovados do dia são ordenados por `score` e enviados ao Telegram. Teto: `MAX_DAILY_NOTIFICATIONS = 3`.

## 6. O agente semanal (LangGraph)

```
preparar_semana
   │  sorteia 2 descartados da semana pra auditoria
   ▼
enviar_auditoria  ──►  Telegram: "Descartei. Confirma?"
   │
   ▼
⏸ aguardar_respostas   (interrupt; retomado pela rotina de respostas quando
   │                     eu respondo, ou sozinho depois de segunda 8h)
   │
   ├── semana sem feedback? ──► congelar_semana ──► enviar_resumo ("nada mudou", seção 4)
   ▼
atualizar_placares     (código puro: decay, posts novos no placar, aposentadoria, promoção)
   │
   ▼
analisar_motivos       (LLM: agrupa meus motivos de ✗ e os erros da auditoria em padrões)
   │
   ▼
propor_ajustes         (LLM com ferramentas, num laço próprio)
   │   tools: consultar_historico, listar_receitas, testar_regra, testar_receita,
   │          registrar_propostas
   ▼
validar_ajustes        (código: guardrails abaixo)
   │
   ▼
aplicar_ajustes  ──►  enviar_resumo  ──►  Telegram: resumo da semana
```

### Como roda no GitHub Actions (sem servidor)

- **Domingo 10h** (`weekly.yml`): `prospector weekly start` registra a semana em `weekly_runs`, roda o grafo até `aguardar_respostas` e para. O `interrupt()` grava o estado no Postgres (`PostgresSaver`) e o processo termina.
- **A cada 2h** (`replies.yml`, job `semanal`, depois de ler as respostas): `prospector weekly resume` olha se há semana pausada. Se respondi os 2 posts da auditoria, ou se passou o prazo (`resume_after`, domingo 10h + 22h = segunda 8h), retoma com `Command(resume=...)`. Quase sempre não há nada pausado, e o comando sai em segundos sem chamar LLM.
- Sem descartes na semana, não há auditoria nem pausa: o grafo vai até o fim no domingo.
- Se um domingo chega com a semana anterior ainda pausada (o Actions ficou fora do ar, por exemplo), `start` termina a anterior antes de começar a nova.
- O `thread_id` do checkpoint é `weekly-<semana>`. A semana é a data da execução (`2026-11-29`).
- **Commit antes do checkpoint.** Cada nó que grava no banco confirma a transação no fim (`deps.commit()`). Se o processo cair entre o commit e o checkpoint, o nó roda de novo na retomada. Por isso `enviar_auditoria` não reenvia o que já foi enviado e o placar não conta um post duas vezes (`scored_posts`).
- As tabelas do `PostgresSaver` (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) são criadas pelo `setup()` dele, e o comando liga o RLS nelas logo depois (seção 10). O estado do grafo (`agent/state.py`) guarda só ids, contagens e conclusões do LLM, nunca texto de post.

### O placar da semana (`atualizar_placares`)

Código puro (`core/scoring.py` + `flows/weekly.py`), nesta ordem:

1. **Esquecimento** (`DECAY`) em todas as receitas;
2. **posts novos no placar**, com peso cheio. Cada post entra uma vez só, na semana em que o resultado dele fica definido. O aprovado espera os 7 dias de prazo pela minha resposta; até lá, fica na fila. Um descarte conta na hora, a não ser que tenha caído na auditoria;
3. **aposentadoria** das ativas que perderam de forma convincente para a líder;
4. **promoção** de no máximo 1 candidata, se abriu vaga.

Esquecer antes de somar faz a evidência nova entrar inteira. `scored_posts` registra o que já contou. A migração que criou a tabela marcou todos os posts que já existiam (o histórico importado) como `before_agent`: eles já estão no prior das receitas, e contar de novo seria contar duas vezes.

### Auditoria

- 2 descartados por semana, sorteados entre os da semana que ainda não foram auditados. **Os que o juiz viu têm preferência** (nota acima de zero): um descarte das regras fixas é quase sempre lixo claro (post velho, carreta), e o que ensina é o descarte do juiz, porque são as regras dele que o agente ajusta.
- Botões: `✓ Descarte certo`, `✗ Era bom, eu chamaria` e `✗ Outro (responda com o motivo)`. A resposta segue as regras da seção 8, com `source = audit`.
- Se a imagem tem cópia guardada, a prévia do link usa um link temporário dela: a URL do Facebook já pode ter expirado.

### Ferramentas do agente

| Ferramenta | O que faz |
|---|---|
| `consultar_historico` | Busca posts que eu julguei, com filtros (dias, receita, trecho de texto, bom/ruim) |
| `listar_receitas` | Receitas com placar, taxa estimada e status |
| `testar_regra` | Roda o juiz com as regras alteradas contra os posts rotulados e compara com as regras atuais (**backtest**, até 30 posts). Máx. 6 por semana |
| `testar_receita` | Busca de teste no Apify com 5 resultados, mostra quantos passam nas regras fixas. Gasta orçamento real (passa pela trava do mês e grava em `search_runs` sem receita); máx. 2 por semana |
| `registrar_propostas` | Entrega as propostas finais e encerra o laço |

As ferramentas (`agent/tools.py`) chamam `flows/weekly.py`, nunca as integrações. Os limites de uso são contados em código, e o LLM é avisado na resposta da ferramenta quando passa deles.

`propor_ajustes` roda seu laço (LLM → ferramentas → LLM) num grafo pequeno e separado, sem checkpoint: as mensagens, que trazem trechos de posts, vivem só durante o laço. Para o grafo da semana, ele é um nó comum que devolve as propostas. O laço tem teto de 40 passos; passou disso, a semana segue sem ajuste e o resumo diz por quê.

### Guardrails (código, não LLM)

O LLM **propõe**; o código **decide** se aplica (`validar_ajustes`, regras em `core/weekly.py`):

- Os únicos ajustes possíveis: regra nova, desligar regra, trocar o texto de uma regra (desliga a antiga e cria a nova; regras são versionadas, nunca editadas) e termo de busca novo, que entra como **candidata**.
- Regra de triagem só entra se o backtest não piorar a taxa de acerto nem fizer o juiz deixar de aprovar mais de 1 post que eu marquei como bom. O `validar_ajustes` mede de novo (reaproveitando a medição da ferramenta quando é o mesmo conjunto de regras): o número que vale é o do código, não o que o LLM disse.
- As mudanças de regra são testadas em sequência: cada uma contra as regras já com as anteriores aceitas.
- No máximo 1 receita nova e 3 mudanças de regra por semana. Termo repetido é barrado.
- O LLM não mexe em placar, orçamento nem status de receita: o formato da proposta não tem como pedir isso.
- Tudo que é aplicado vai pro `changelog` com o motivo. O que foi barrado aparece no resumo, com o porquê.

**Por que ajustes automáticos:** eu não aprovo antes; avalio o efeito na semana seguinte. O botão de desfazer é a rede de segurança.

### O LLM do agente

- **LangChain + LangGraph** só aqui (seção 9). O modelo é o `ChatAnthropic`, montado no `cli.py`: `claude-opus-5-5` (`AGENT_MODEL`), esforço `high`, e fallback de recusa do lado do servidor ligado (`fallbacks: "default"`): se o Claude recusar, outro modelo continua na mesma chamada.
- **A porta do LLM é a interface `BaseChatModel` do LangChain.** Os nós recebem um `BaseChatModel` pronto; o fake dos testes é um modelo roteirizado (`ScriptedChatModel`, em `integrations/fakes.py`), que devolve respostas e chamadas de ferramenta pré-definidas. Assim `agent/` não importa `langchain_anthropic`, e o grafo inteiro roda nos testes sem rede.
- As duas etapas de LLM entregam o resultado chamando uma ferramenta (`registrar_padroes`, `registrar_propostas`), com escolha de ferramenta automática: o Opus 5.5 não aceita forçar uma ferramenta. Se ele responder em texto em vez de chamar, o texto vira o padrão da semana e o laço segue.
- Se não há motivo meu nem erro de auditoria na semana, o LLM nem é chamado. Erro de API vira "o agente não conseguiu propor ajustes" no resumo; o placar já foi atualizado e nada mais muda.
- **Custo estimado:** uma semana com ajustes faz algo como 5 a 15 chamadas ao Opus (US$ 4 / US$ 20 por milhão de tokens) e até umas 360 chamadas do juiz no Haiku nos testes de regra (6 testes × 30 posts × 2 conjuntos de regras, com cache do prompt). Fica na ordem de US$ 0,50 a 1 por semana.

## 7. Orçamento

Apify, actor `scraper_one/facebook-posts-search`, plano Free: US$ 5/mês de crédito ≈ 1.000 posts. O crédito não acumula e, quando acaba, o Apify bloqueia (não cobra).

| Parâmetro | Valor |
|---|---|
| `MONTHLY_RESULT_BUDGET` | 900 posts |
| `DAILY_RESULT_BUDGET` | `(orçamento restante no mês) / (dias restantes)`, teto 40 |
| Testes do agente (`testar_receita`) | Saem do mesmo orçamento |

O orçamento é uma **trava em código**. O agente decide como distribuir, nunca quanto gastar acima do teto.

- **Fonte do gasto:** cada chamada ao Apify grava uma linha em `search_runs` (pedido e cobrado). "Gasto do mês" é a soma do cobrado desde o dia 1º.
- **Trava antes de cada busca**, não só no começo da coleta: se uma busca cobrar mais que o pedido, a próxima é barrada.
- O Apify cobra todo item devolvido, inclusive o que não vira post (sem data, sem texto nem imagem).
- `prospector collect --dry-run --live` (teste da integração, até 10 resultados) não grava posts, mas **grava o gasto**: o crédito foi consumido e a trava precisa saber.

## 8. Telegram

**Mensagem de post aprovado:**

```
Novo post aprovado
"Procuro agregado com truck pra rota Guarulhos–Campinas..."

Guarulhos → Campinas · há 3h · receita: agregado truck
Por que aprovei: truck, dentro da região, carga seca
Abrir post                       (link no texto)
[✓ Certo]
[✗ Fora da região]
[✗ Não é oportunidade]
[✗ Veículo não serve]
[✗ Outro (responda com o motivo)]
```

- Os botões de ✗ mostram os 3 motivos mais frequentes das últimas 4 semanas (só respostas a posts aprovados). Enquanto não houver 3, completam com "Fora da região", "Não é oportunidade" e "Veículo não serve". A lista oferecida fica gravada com a mensagem (`notifications.reject_reasons`): o botão carrega só o índice (`w0`, `w1`, `w2`), e o motivo é o que estava na tela, mesmo que a lista mude depois.
- O link do post vai no texto, não num botão: depois que a resposta é gravada, os botões viram um só, inerte ("Registrado: ✗ Fora da região"), e o link continua lá.
- Post com imagem mostra a imagem na prévia do link. Sem imagem, a prévia fica desligada (a do Facebook é a tela de login).
- O envio acontece depois que a coleta foi gravada: se o Telegram falhar, posts e gasto não se perdem. Um envio que falha não impede os outros.

### Como uma resposta vira `Feedback`

| O que eu faço | O que é gravado |
|---|---|
| Toco ✓ | Acertou |
| Toco ✗ com motivo | Errou, com aquele motivo |
| Toco "Outro" | Nada ainda: espera o motivo |
| Respondo (reply) à mensagem com texto | Errou, com o texto como motivo. Se eu também toquei ✓, é acerto, e o texto vira o motivo do acerto ("rota boa") |
| Toco mais de um botão antes da leitura | Vale o último (corrigi um toque errado) |
| Respondo de novo depois que a resposta foi gravada | Ignorado. Vale a primeira resposta gravada, e o celular mostra essa |

As regras moram em `core/notification.py`, sem Telegram, com testes. Só conta o que vem do meu chat (`TELEGRAM_CHAT_ID`): qualquer pessoa pode achar o bot e mandar mensagem.

### Recebimento

- **Sem servidor.** A rotina de respostas usa `getUpdates` a cada 2h. O Telegram guarda atualizações por até 24h. O bot não pode ter webhook configurado, senão o `getUpdates` é recusado.
- **Consequência:** o toque no botão não tem reação imediata; a confirmação ("Registrado: ...") aparece na próxima leitura, em até ~2h.
- **Offset no banco** (`app_state`), gravado na mesma transação das respostas. O Telegram só descarta uma atualização quando a leitura seguinte pede um offset depois dela. Se a gravação falhar, a próxima rodada lê tudo de novo, e gravar duas vezes não duplica (`UNIQUE (post_id, source)`).
- A confirmação no celular só acontece depois do commit. Senão o celular diria "registrado" sobre algo que o banco desfez.
- Melhoria futura: webhook num Cloudflare Worker, pra resposta instantânea.

### Privacidade nos logs

O repositório é público, e o log do GitHub Actions também. Nos workflows, `collect` e `ingest-replies` rodam com `--summary`: só números, nenhum texto de post. O token do bot vai na URL da Bot API, então todo erro de rede é traduzido sem a URL antes de subir.

## 9. Arquitetura de código (arquitetura limpa)

```
src/prospector/
├── cli.py           ponto de montagem: escolhe a implementação de cada porta
├── core/            regras de negócio puras, SEM dependências externas
│   ├── post.py          Post (texto e/ou imagem), TriageResult
│   ├── recipe.py        Recipe (termo, params, alpha, beta, status)
│   ├── feedback.py      Feedback (post, veredito, motivo, origem), semana sem resposta
│   ├── bandit.py        Thompson Sampling, decay, aposentadoria
│   ├── scoring.py       o placar da semana: o que cada post valeu para as receitas
│   ├── weekly.py        semana do agente: auditoria, pausa, ajustes e guardrails
│   ├── budget.py        cálculo do orçamento diário
│   ├── rules.py         regras determinísticas da triagem
│   ├── triage.py        regras do juiz, exemplos few-shot e a escolha deles
│   ├── notification.py  botões de motivo e como uma resposta vira Feedback
│   ├── region.py        cidades conhecidas e raio a partir da base
│   └── text.py          normalização de texto
├── flows/           casos de uso: o que o sistema faz
│   ├── ports.py         interfaces: Repository, LegacySource, SearchProvider, Judge, Messenger, ImageStore
│   ├── collect.py       coleta e triagem diária
│   ├── backtest.py      mede regras e juiz contra as minhas respostas
│   ├── notify.py        manda os aprovados pro Telegram e grava cada mensagem
│   ├── ingest_replies.py lê meus ✓/✗, grava as respostas e o offset
│   ├── weekly.py        passos do agente semanal que não são LLM (auditoria, placar,
│   │                    backtest de regra, busca de teste, ajustes, resumo)
│   └── import_legacy.py
├── agent/           o agente semanal (LangGraph + LangChain)
│   ├── graph.py         o grafo: nós e arestas
│   ├── state.py         o estado que o checkpoint grava
│   ├── nodes.py         os nós, os prompts e o laço de ferramentas
│   ├── tools.py         tools chamam os flows, nunca as integrações direto
│   ├── deps.py          as portas que o cli.py entrega ao grafo
│   └── runner.py        começar e retomar a semana
└── integrations/    implementações concretas das portas
    ├── apify.py         SearchProvider sobre o actor do Apify
    ├── fakes.py         busca, juiz, Telegram e LLM do agente falsos (dry-run e testes)
    ├── samples/         posts de exemplo, inventados, no formato do Apify
    ├── telegram.py      Messenger sobre a Bot API (sendMessage, getUpdates)
    ├── supabase_storage.py ImageStore: cópia das imagens no bucket privado
    ├── postgres.py      Repository sobre Postgres + migrador
    ├── sql/             migrações numeradas (001_initial, 002_search_runs, ..., 007_weekly_agent)
    ├── memory.py        Repository em memória: fake dos testes e do --dry-run
    ├── legacy_sqlite.py LegacySource sobre o prospector.db antigo
    └── llm.py           juiz: Claude via SDK oficial da Anthropic (saída estruturada)
```

**Regra de dependência:** `integrations → flows → core`. O `core` não importa nada de fora. Por isso o bandit e o orçamento são testáveis em milissegundos, sem internet.

**Ponto de montagem:** só o `cli.py` conhece flows e integrações ao mesmo tempo. Ele escolhe a implementação concreta de cada porta (Postgres ou memória, por exemplo) e a entrega ao caso de uso.

**Fakes honestos:** toda porta tem uma implementação fake. A do `Repository` passa pelos mesmos testes que a do Postgres (`tests/test_repository_contract.py`), para o fake não se comportar diferente do banco real.

**Modelos:** `claude-haiku-5-5` para o juiz diário (pequeno e barato, `effort: low`); `claude-opus-5-5` para o agente semanal (seção 6). LangChain/LangGraph entram só no agente semanal, onde há grafo, ferramentas e pausa; o juiz usa o SDK oficial direto.

**A porta do LLM no agente** é a interface `BaseChatModel` do LangChain, e não um `Protocol` em `ports.py`: ela já é uma interface, com uma implementação real (`ChatAnthropic`, montada no `cli.py`) e uma fake (`ScriptedChatModel`). Recriar a mesma coisa em `ports.py` só duplicaria o LangChain. `agent/` continua sem importar `integrations/` nem `langchain_anthropic`.

## 10. Infraestrutura

| Peça | Escolha | Por quê |
|---|---|---|
| Execução | GitHub Actions | Gratuito e ilimitado em repositório público |
| Banco | PostgreSQL no Supabase (free) | Actions não tem disco permanente; LangGraph tem `PostgresSaver` pronto |
| Imagens | Supabase Storage (free, 1 GB), bucket privado | Mesmo projeto do banco; a URL do Facebook expira |
| Segredos | GitHub Secrets | Nunca no código |

**Banco:**

- O schema vive em migrações numeradas (`integrations/sql/`), aplicadas com `prospector db migrate`. Migração aplicada não se edita; mudança nova é arquivo novo.
- **RLS ligado em todas as tabelas, sem policies.** O Supabase expõe o schema `public` pela API REST; assim ela não lê nada, e a conexão direta do sistema (dona das tabelas) continua funcionando. Os posts têm nome e telefone de gente real.
- O GitHub Actions não tem IPv6, e a conexão direta do Supabase (`db.<projeto>.supabase.co`) só tem IPv6. Nos workflows, o `DATABASE_URL` precisa ser o do **pooler em modo sessão** (`aws-0-<região>.pooler.supabase.com:5432`).
- O CI sobe um Postgres descartável para os testes de contrato e de migração. Cada teste roda num schema temporário, apagado no fim.

**Workflows:**

| Arquivo | Quando (BRT) | Faz |
|---|---|---|
| `collect.yml` | Todo dia, 7h | Coleta, triagem e envio dos aprovados |
| `replies.yml` | A cada 2h | Ingere respostas; depois, o job `semanal` retoma o grafo semanal se for a hora |
| `weekly.yml` | Domingo, 10h | Começa o agente semanal (auditoria e pausa) |
| `ci.yml` | Todo PR | Lint + testes |

Cron do Actions é em UTC (BRT + 3h) e pode atrasar alguns minutos.

- **Chave liga/desliga.** Os agendamentos só rodam quando a variável do repositório `PROSPECTOR_ENABLED` vale `true` (`gh variable set PROSPECTOR_ENABLED --body true`; para desligar, `--body false`). Com a chave desligada, o job agendado é pulado, e não falha. O botão "Run workflow" funciona sempre, para testar antes de ligar. Por quê: o sistema fica pronto no `main` sem gastar Apify nem LLM até eu decidir ligar, e sem um e-mail de falha a cada 2h enquanto os secrets não estão cadastrados.
- **Secrets** usados pelos workflows: `DATABASE_URL` (pooler), `APIFY_TOKEN`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
- Cada workflow aplica as migrações pendentes antes de rodar (`prospector db migrate` não faz nada quando o banco está em dia).
- `concurrency` impede duas coletas ao mesmo tempo, que leriam o mesmo gasto do mês e poderiam passar juntas do teto. O `weekly.yml` e o job `semanal` do `replies.yml` dividem o grupo `weekly`: começar e retomar a mesma semana ao mesmo tempo mandaria a auditoria duas vezes.
- **Checkpoints do LangGraph:** `prospector weekly` abre uma segunda conexão (autocommit, linhas como dicionário, `prepare_threshold=0` por causa do pooler) para o `PostgresSaver`, roda o `setup()` dele, que cria as tabelas de checkpoint na primeira vez, e liga o RLS nelas.
- O GitHub desliga os agendamentos de um repositório público depois de 60 dias sem commit. Se a coleta parar sozinha, é isso: reative em Actions.

## 11. Histórico do FacebookProspector

O banco antigo (`prospector.db`, SQLite) tem o que precisamos:

| Tabela/campo antigo | Uso aqui |
|---|---|
| `posts.search_query` | Liga cada post à receita de origem |
| `posts.user_action` = `CHAMEI` | Acerto |
| `posts.user_action` = `NAO_CHAMEI` | Erro |
| `posts.processing_status` = `FILTERED` | Erro (lixo) |
| `posts.user_action_reason` | Motivo → exemplos few-shot e regras iniciais |
| `post_search_matches` | Posts que apareceram em mais de uma busca |

**Importação (`flows/import_legacy.py`):**

- Cada post antigo é traduzido para o vocabulário novo e pontuado pela mesma `recipe_outcome` do `core`, sem regra de contagem própria:
  - `FILTERED` vira descarte; qualquer outro status foi para a fila, então vira aprovação.
  - `CHAMEI`/`NAO_CHAMEI` diz se o post era bom. O `Verdict` é "certo" quando decisão e post concordam. Um post filtrado que eu chamei mesmo assim vira auditoria com "errou".
  - `PULAR` era adiamento, não decisão: conta como sem resposta.
- **Corte na minha última resposta.** Só entra o que foi coletado até a data da minha última resposta (`--until` muda o corte). Depois disso eu parei de olhar e o sistema antigo seguiu coletando sozinho: todo descarte viraria erro sem eu ter dito nada. É a regra da semana sem feedback (seção 4), aplicada ao legado. Uma busca que reencontrou um post antigo depois do corte também não conta para ele.
- Posts só de imagem entram (seção 5). Post sem texto e sem imagem fica de fora.
- Buscas que eram URL de grupo (coletor de grupos, fora do escopo) não viram receita; os posts delas entram como histórico.
- Cada `search_query` antigo vira uma receita com prior `Beta(1 + k·acertos, 1 + k·erros)`, com `k = 0.2` (dado velho vale menos).
- As 4 receitas com maior taxa esperada começam **ativas**, mas só entre as que têm **pelo menos 20 posts** no histórico; as outras ficam **candidatas**. Sem esse mínimo, uma busca testada uma vez, com 3 posts, teria taxa perto de 0.5 (quase só incerteza) e tiraria a vaga de uma com 60 posts e 10 acertos. Se faltarem receitas com 20 posts, as melhores restantes completam as vagas.
- Reexecutável: só insere o que não existe. Reimportar não duplica posts nem reseta o placar de uma receita que já aprendeu.
- Comando: `prospector import-legacy <caminho> [--dry-run]`. O arquivo é aberto só para leitura.
- Posts rotulados viram base de exemplos e de backtest.
- O agente roda uma vez `analisar_motivos` sobre o histórico para gerar as **regras de triagem iniciais**.
- Campos de autor (nome, perfil) nem são lidos: não são necessários para aprender.
- **De onde vem o arquivo:** o banco de verdade está no servidor antigo (`/opt/prospector/prospector.db`). A cópia em `C:\FacebookProspector` é de um teste local de uma noite só. Copie com `sqlite3 ... '.backup ...'` (seguro com o serviço rodando) para fora do repositório.

## 12. Fora do escopo (por enquanto)

- RAG
- Coletor de grupos (`apify/facebook-groups-scraper`)
- Interface web
- Multiusuário
