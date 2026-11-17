# Prospector Agente

Agente que procura oportunidades de **agregamento de caminhão e frete** em posts públicos do Facebook, descarta o lixo e manda só o que vale a pena pro meu celular. Toda resposta minha (✓ certo / ✗ errado + motivo) ensina o agente a buscar melhor e a filtrar melhor.

> Sucessor do [FacebookProspector](https://github.com/pegorarogabriel/FacebookProspector), que coletava e classificava mas dependia de eu revisar tudo à mão.

## Como funciona em 30 segundos

```
 TODO DIA                         TODA SEMANA (domingo)
 ─────────                        ─────────────────────
 1. Escolhe onde buscar           5. Manda 2 posts descartados: "descartei, confirma?"
 2. Busca ~30 posts (Apify)       6. Atualiza o placar de cada busca
 3. Joga fora o lixo              7. Lê meus motivos e ajusta buscas e regras
 4. Me manda os aprovados         8. Me manda o resumo da semana
    no Telegram
         │                                     ▲
         └──── eu toco ✓ ou ✗ + motivo ────────┘
```

- **Escolher onde buscar:** cada termo de busca tem um placar de acertos e erros. Termos que acertam ganham mais orçamento; termos novos ganham algumas chances pra provar valor. ([por quê](docs/arquitetura.md#3-como-o-agente-escolhe-onde-buscar))
- **Jogar fora o lixo:** regras fixas (idade do post, região, duplicados) + um LLM que lê o post e justifica a decisão.
- **Aprender:** um agente em LangGraph lê meus motivos de rejeição e propõe ajustes, testando cada ajuste contra o histórico antes de aplicar.

## Onde está cada coisa

| Quero entender... | Vá para |
|---|---|
| A arquitetura completa e as decisões | [`docs/arquitetura.md`](docs/arquitetura.md) |
| As regras de negócio (post, busca, placar) | [`src/prospector/core/`](src/prospector/core/) |
| O que o sistema faz (coletar, triar, aprender) | [`src/prospector/flows/`](src/prospector/flows/) |
| O agente semanal (LangGraph) | [`src/prospector/agent/`](src/prospector/agent/) |
| Conexões externas (Apify, Telegram, banco, LLM) | [`src/prospector/integrations/`](src/prospector/integrations/) |
| Quando cada rotina roda | [`.github/workflows/`](.github/workflows/) |

## Stack

Python 3.12 · LangGraph + LangChain · Claude (Anthropic) · Apify · Telegram Bot API · PostgreSQL (Supabase) · GitHub Actions

## Rodando localmente

```bash
uv sync
cp .env.example .env   # preencha as chaves
uv run pytest
uv run prospector collect --dry-run
```

## Privacidade

Os posts coletados contêm nomes e contatos de pessoas reais. **Nenhum dado é versionado neste repositório.** Tudo fica no banco externo; chaves ficam nos Secrets do GitHub.
