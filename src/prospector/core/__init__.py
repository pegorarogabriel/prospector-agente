"""Regras de negócio puras: Recipe, Post, Feedback, bandit e orçamento.

Esta camada NÃO importa nada externo — nem LangChain, nem SQLAlchemy, nem
requests. É o que a mantém testável em milissegundos, sem internet.
O teste `tests/test_architecture.py` falha se essa regra for quebrada.
"""
