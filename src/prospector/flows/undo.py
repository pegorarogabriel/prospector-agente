"""O botão "Desfazer ajustes": reverte as regras e buscas que o agente mudou na semana.

Ver `docs/arquitetura.md` seção 6. É a rede de segurança dos ajustes
automáticos: eu não aprovo antes, avalio depois, e se não gostei, desfaço.

O que é revertido, ajuste por ajuste do `changelog`, do último para o primeiro:

- regra nova → desligada;
- regra desligada → ligada de novo;
- busca nova → aposentada (se ainda não estava).

O placar não muda. Ele é conta sobre o que aconteceu, não um ajuste do agente.

Desfazer duas vezes não faz nada na segunda: cada ajuste revertido fica
marcado (`reverted_at`), e a semana também (`undone_at`).
"""

from dataclasses import dataclass, field, replace
from datetime import datetime

from prospector.core.recipe import RecipeStatus
from prospector.core.weekly import Change, ChangeKind
from prospector.flows.ports import Repository


@dataclass(frozen=True)
class UndoReport:
    week_id: str
    reverted: list[Change] = field(default_factory=list)
    already_undone: bool = False


def undo_week(repository: Repository, week_id: str, *, now: datetime) -> UndoReport:
    run = repository.get_weekly_run(week_id)
    if run is None:
        return UndoReport(week_id)
    if run.undone_at is not None:
        return UndoReport(week_id, already_undone=True)

    recipes = {recipe.id: recipe for recipe in repository.list_recipes()}
    reverted: list[Change] = []
    for change in reversed(repository.list_changes(week_id)):
        if change.reverted_at is not None:
            continue
        if change.kind is ChangeKind.ADD_RULE:
            repository.set_rule_active(change.target_id, False)
        elif change.kind is ChangeKind.DEACTIVATE_RULE:
            repository.set_rule_active(change.target_id, True)
        elif change.kind is ChangeKind.ADD_RECIPE:
            recipe = recipes.get(change.target_id)
            if recipe is not None and recipe.status is not RecipeStatus.RETIRED:
                repository.update_recipes([replace(recipe, status=RecipeStatus.RETIRED)])
        reverted.append(change)

    repository.mark_changes_reverted([c.id for c in reverted if c.id is not None], now)
    repository.update_weekly_run(replace(run, undone_at=now))
    return UndoReport(week_id, reverted=reverted)
