# Instruções para o Claude Code

Leia `README.md` e `docs/arquitetura.md` antes de qualquer tarefa. A arquitetura descrita lá é a fonte da verdade; se algo precisar mudar, proponha a mudança no documento junto com o código.

## Sobre o dono do projeto

Gabriel está aprendendo Git, arquitetura limpa, LangChain e LangGraph com este projeto. Ao final de cada etapa:
- explique em português, em poucos parágrafos, o que foi feito e por quê;
- aponte 1 ou 2 conceitos que valem a pena ele estudar a partir do que foi feito.

## Convenções

- Python 3.12, gerenciado com `uv`. Lint e formatação com `ruff`. Testes com `pytest`.
- Type hints em tudo. Código e nomes em inglês; docs e mensagens ao usuário em português.
- `src/prospector/core/` não importa nada externo (nem LangChain, nem SQLAlchemy, nem requests).
- `agent/tools.py` chama `flows/`, nunca `integrations/` diretamente.
- Toda chamada externa (Apify, Telegram, LLM, banco) passa por uma porta em `flows/ports.py` e tem implementação fake para testes.

## Git

- Uma branch e um PR por etapa do roadmap. Commits pequenos, mensagens em português no imperativo ("Adiciona bandit com decay").
- Nunca commitar `.env`, bancos, dumps, posts ou qualquer dado coletado. O repositório é público.

## Segurança e custo

- Nunca rode o Apify real em testes. Use o fake.
- O teto `MONTHLY_RESULT_BUDGET` é verificado em código antes de toda chamada ao Apify.

## Roadmap

| Etapa | Entrega | Pronto quando |
|---|---|---|
| 0 | Setup: `uv`, `ruff`, `pytest`, estrutura de pastas, `.env.example`, `ci.yml` | CI verde num PR vazio |
| 1 | `core/`: Recipe, Post, Feedback, bandit (Thompson + decay + aposentadoria), budget | Testes cobrindo sorteio, decay e aposentadoria |
| 2 | Banco: schema Postgres, `integrations/postgres.py`, `import_legacy.py` | Histórico importado; receitas com prior |
| 3 | Coleta: `integrations/apify.py`, regras determinísticas portadas do projeto antigo, `flows/collect.py` com juiz fake | `prospector collect --dry-run` funciona |
| 4 | Juiz LLM: prompt com regras + few-shot, saída estruturada | Backtest do juiz contra o histórico com relatório de acerto |
| 5 | Telegram: envio com botões, `ingest_replies.py`, `replies.yml`, `collect.yml` | Recebo post real no celular e meu ✓/✗ chega no banco |
| 6 | Agente semanal: grafo, tools, guardrails, interrupt com `PostgresSaver`, `weekly.yml` | Uma semana completa rodando sozinha |
| 7 | Resumo semanal + botão Desfazer ajustes | Desfazer reverte regras e receitas da semana |
