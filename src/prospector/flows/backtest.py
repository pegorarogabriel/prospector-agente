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
from dataclasses import dataclass, field
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
    """Posts só de imagem: a URL antiga expirou, então não há o que julgar."""

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


def run_backtest(
    labeled: Sequence[LabeledPost],
    make_judge: Callable[[list[FewShotExample]], Judge],
    *,
    limit: int | None = None,
    examples_good: int = EXAMPLES_GOOD,
    examples_bad: int = EXAMPLES_BAD,
) -> BacktestReport:
    """Roda regras e juiz em cada post rotulado e mede contra o gabarito.

    A regra de idade usa a data do próprio post como "agora": o histórico é de
    setembro e outubro, e sem isso todo post cairia por ser velho.
    """
    examples = choose_examples(labeled, good=examples_good, bad=examples_bad)
    example_ids = {example.post_id for example in examples}
    judge = make_judge(examples)

    candidates = [item for item in labeled if item.post.id not in example_ids]
    image_only = [item for item in candidates if not item.post.text.strip()]
    measurable = [item for item in candidates if item.post.text.strip()]
    chosen = _balanced(measurable, limit) if limit is not None else measurable

    cases: list[BacktestCase] = []
    for item in chosen:
        good = post_was_good(approved=item.decision.approved, feedback=item.feedback)
        verdict = apply_rules(item.post, now=item.post.posted_at + timedelta(hours=1))
        judged = judge.judge(item.post, verdict) if verdict.keep else None
        cases.append(BacktestCase(item.post, good, item.feedback.reason or "", verdict, judged))

    return BacktestReport(
        cases=cases,
        examples=examples,
        skipped_image_only=len(image_only),
        skipped_by_limit=len(measurable) - len(chosen),
        rules=Confusion.of([(case.good, case.rules.keep) for case in cases]),
        judge=Confusion.of(
            [(case.good, case.approved) for case in cases if case.judged is not None]
        ),
        combined=Confusion.of([(case.good, case.approved) for case in cases]),
    )
