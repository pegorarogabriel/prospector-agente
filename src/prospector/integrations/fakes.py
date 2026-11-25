"""Implementações falsas das portas `SearchProvider` e `Judge`.

Servem aos testes e ao `prospector collect --dry-run`: o caminho inteiro da
coleta roda sem gastar crédito do Apify nem chamar LLM.

Os posts de exemplo (`samples/posts.json`) são inventados, no formato real do
actor, e cobrem cada caminho das regras: oferta boa, só imagem, venda,
espanhol, veículo incompatível, fora de SP, post velho, sem data.
"""

import hashlib
import json
import random
from collections.abc import Mapping
from datetime import datetime, timedelta
from importlib import resources
from typing import Any

from prospector.core.post import Post, TriageResult
from prospector.core.rules import RuleVerdict
from prospector.flows.ports import SearchResult
from prospector.integrations.apify import parse_item


def _load_samples() -> list[dict[str, Any]]:
    path = resources.files("prospector.integrations") / "samples" / "posts.json"
    return json.loads(path.read_text(encoding="utf-8"))


class SampleSearchProvider:
    """Devolve posts de exemplo como se viessem do Apify.

    As datas são relativas a `now` (cada exemplo diz há quantas horas foi
    postado), para os exemplos não envelhecerem e caírem na regra de idade.
    Cada busca sorteia uma amostra estável por termo: buscas diferentes se
    sobrepõem em parte, o que exercita a junção de duplicados.
    """

    def __init__(self, *, now: datetime, items: list[dict[str, Any]] | None = None) -> None:
        self._now = now
        self._items = items if items is not None else _load_samples()

    def _as_apify_item(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        item = {key: value for key, value in sample.items() if not key.startswith("_")}
        hours = item.pop("hours_ago", None)
        if hours is not None:
            moment = self._now - timedelta(hours=float(hours))
            item["timestamp"] = int(moment.timestamp() * 1000)
        return item

    def search(
        self, query: str, params: Mapping[str, str], *, limit: int, since: datetime
    ) -> SearchResult:
        pool = [self._as_apify_item(sample) for sample in self._items]
        rng = random.Random(query)
        chosen = rng.sample(pool, k=min(limit, len(pool)))
        posts = [post for item in chosen if (post := parse_item(item))]
        return SearchResult(posts=posts, charged=len(chosen))


class FakeJudge:
    """Aprova tudo que passou nas regras, com uma nota estável por post.

    A nota varia entre 0.50 e 0.90 (hash do id) só para o dry-run mostrar a
    ordenação dos aprovados e o corte das notificações. Não é uma opinião.
    """

    def judge(self, post: Post, verdict: RuleVerdict) -> TriageResult:
        digest = int(hashlib.sha256(post.id.encode()).hexdigest()[:8], 16)
        score = round(0.5 + 0.4 * (digest % 1000) / 1000, 2)
        reason = "juiz falso: passou nas regras"
        if verdict.driver_offering:
            reason += " (parece motorista se oferecendo)"
        return TriageResult(
            approved=True,
            score=score,
            reason=reason,
            vehicle=verdict.vehicle,
            origin=verdict.region.origin,
            destination=verdict.region.destination,
        )
