"""Implementações concretas das portas: Apify, Telegram, Postgres e LLM.

Camada mais externa: pode importar `flows` e `core`, e é a única que fala com
o mundo. Toda porta daqui tem uma implementação fake para testes.
"""
