"""Backtest do juiz: as duas camadas da triagem contra as minhas respostas reais.

Ver `docs/arquitetura.md` seção 5. O gabarito de cada post é "eu teria
chamado?", que sai da decisão + do meu veredito (`core.feedback.post_was_good`).

Três recortes, porque cada um responde a uma pergunta diferente:

- **regras**: o filtro fixo, sozinho. Quanto lixo ele tira, e quanto post bom
  ele joga fora por engano;
- **juiz**: só nos posts que passaram nas regras. É o trabalho do LLM;
- **as duas camadas**: o que de fato chegaria ao celular.

Os posts escolhidos como exemplo few-shot ficam fora da medição: o juiz não
pode ser medido em cima de posts cuja resposta está no próprio prompt.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta

from prospector.core.feedback import post_was_good
from prospector.core.post import Post, TriageResult
from prospector.core.rules import RuleVerdict, apply_rules
from prospector.core.triage import FewShotExample, select_examples
from prospector.flows.ports import Judge, LabeledPost

#: Exemplos no prompt: mais "não chamei" porque os motivos de recusa variam mais.
EXAMPLES_GOOD = 4
EXAMPLES_BAD = 8


def to_example(labeled: LabeledPost) -> FewShotExample:
    """Uma resposta minha como exemplo do juiz.

    Nos "chamei" eu quase nunca escrevia motivo: o próprio "chamei" é o motivo.
    Nos "não chamei", sem motivo o exemplo não ensina nada e fica de fora.
    """
    good = post_was_good(approved=labeled.decision.approved, feedback=labeled.feedback)
    reason = (labeled.feedback.reason or "").strip() or ("eu chamei" if good else "")
    return FewShotExample(
        post_id=labeled.post.id,
        post_text=labeled.post.text,
        good=good,
        reason=reason,
        answered_at=labeled.feedback.answered_at,
    )


def choose_examples(
    labeled: Sequence[LabeledPost], *, good: int = EXAMPLES_GOOD, bad: int = EXAMPLES_BAD
) -> list[FewShotExample]:
    """Os exemplos do juiz, a partir das minhas respostas."""
    return select_examples([to_example(item) for item in labeled], good=good, bad=bad)


@dataclass(frozen=True)
class Confusion:
    """Matriz de confusão para a pergunta "vale o contato?"."""

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0

    @classmethod
    def of(cls, pairs: Sequence[tuple[bool, bool]]) -> "Confusion":
        """`pairs` é uma lista de (gabarito, previsão)."""
        return cls(
            true_positive=sum(1 for good, predicted in pairs if good and predicted),
            false_positive=sum(1 for good, predicted in pairs if not good and predicted),
            false_negative=sum(1 for good, predicted in pairs if good and not predicted),
            true_negative=sum(1 for good, predicted in pairs if not good and not predicted),
        )

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.false_negative + self.true_negative

    @property
    def accuracy(self) -> float | None:
        hits = self.true_positive + self.true_negative
        return hits / self.total if self.total else None

    @property
    def precision(self) -> float | None:
        """Do que foi aprovado, quanto era bom. Baixa = celular cheio de lixo."""
        approved = self.true_positive + self.false_positive
        return self.true_positive / approved if approved else None

    @property
    def recall(self) -> float | None:
        """Do que era bom, quanto foi aprovado. Baixa = oportunidade perdida."""
        good = self.true_positive + self.false_negative
        return self.true_positive / good if good else None


@dataclass(frozen=True)
class BacktestCase:
    post: Post
    good: bool
    my_reason: str
    rules: RuleVerdict
    judged: TriageResult | None
    """`None` quando as regras já descartaram (o juiz nem foi chamado)."""

    @property
    def approved(self) -> bool:
        return self.judged is not None and self.judged.approved


@dataclass(frozen=True)
class BacktestReport:
    cases: list[BacktestCase]
    examples: list[FewShotExample]
    skipped_image_only: int
    """Posts só de imagem e sem cópia guardada: a URL antiga expirou, não há o que julgar."""

    stored_images: int = 0
    """Posts julgados com a cópia da imagem no lugar da URL expirada."""

    skipped_by_limit: int = 0
    rules: Confusion = field(default_factory=Confusion)
    judge: Confusion = field(default_factory=Confusion)
    combined: Confusion = field(default_factory=Confusion)

    @property
    def judge_errors(self) -> list[BacktestCase]:
        return [
            case
            for case in self.cases
            if case.judged is not None and case.judged.approved != case.good
        ]

    @property
    def rule_errors(self) -> list[BacktestCase]:
        """Posts bons que as regras jogaram fora."""
        return [case for case in self.cases if case.judged is None and case.good]


def _balanced(cases: Sequence[LabeledPost], limit: int) -> list[LabeledPost]:
    """Até `limit` casos, alternando bons e ruins, para a amostra não vir só de ruins."""
    newest = sorted(cases, key=lambda item: item.feedback.answered_at, reverse=True)
    good = [item for item in newest if to_example(item).good]
    bad = [item for item in newest if not to_example(item).good]
    chosen: list[LabeledPost] = []
    while len(chosen) < limit and (good or bad):
        if good:
            chosen.append(good.pop(0))
        if bad and len(chosen) < limit:
            chosen.append(bad.pop(0))
    return chosen


def _with_stored_image(post: Post, image_link: Callable[[str], str | None] | None) -> Post:
    """Troca a URL do Facebook (que expira) por um link para a cópia guardada."""
    if post.image_path is None or image_link is None:
        return post
    link = image_link(post.image_path)
    return replace(post, image_url=link) if link else post


def run_backtest(
    labeled: Sequence[LabeledPost],
    make_judge: Callable[[list[FewShotExample]], Judge],
    *,
    image_link: Callable[[str], str | None] | None = None,
    limit: int | None = None,
    examples_good: int = EXAMPLES_GOOD,
    examples_bad: int = EXAMPLES_BAD,
) -> BacktestReport:
    """Roda regras e juiz em cada post rotulado e mede contra o gabarito.

    A regra de idade usa a data do próprio post como "agora": o histórico é de
    setembro e outubro, e sem isso todo post cairia por ser velho.

    `image_link` dá ao juiz a cópia guardada da imagem. Com ela, post só de
    imagem também é medido; sem ela, ele é pulado.
    """
    examples = choose_examples(labeled, good=examples_good, bad=examples_bad)
    example_ids = {example.post_id for example in examples}
    judge = make_judge(examples)

    candidates = [item for item in labeled if item.post.id not in example_ids]

    def judgeable(item: LabeledPost) -> bool:
        has_copy = image_link is not None and item.post.image_path is not None
        return bool(item.post.text.strip()) or has_copy

    image_only = [item for item in candidates if not judgeable(item)]
    measurable = [item for item in candidates if judgeable(item)]
    chosen = _balanced(measurable, limit) if limit is not None else measurable

    cases: list[BacktestCase] = []
    stored = 0
    for item in chosen:
        good = post_was_good(approved=item.decision.approved, feedback=item.feedback)
        post = _with_stored_image(item.post, image_link)
        verdict = apply_rules(post, now=post.posted_at + timedelta(hours=1))
        judged = judge.judge(post, verdict) if verdict.keep else None
        stored += judged is not None and post.image_url != item.post.image_url
        cases.append(BacktestCase(post, good, item.feedback.reason or "", verdict, judged))

    return BacktestReport(
        cases=cases,
        examples=examples,
        skipped_image_only=len(image_only),
        stored_images=stored,
        skipped_by_limit=len(measurable) - len(chosen),
        rules=Confusion.of([(case.good, case.rules.keep) for case in cases]),
        judge=Confusion.of(
            [(case.good, case.approved) for case in cases if case.judged is not None]
        ),
        combined=Confusion.of([(case.good, case.approved) for case in cases]),
    )
