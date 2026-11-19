"""Testes da trava de orçamento do Apify.

O crédito do plano Free não acumula e não é cobrado quando acaba — o custo de
estourar não é financeiro, é ficar sem coleta no fim do mês. Por isso toda conta
aqui arredonda para baixo e nunca devolve número negativo.
"""

from datetime import date

import pytest

from prospector.core.budget import (
    MAX_DAILY_RESULTS,
    MONTHLY_RESULT_BUDGET,
    can_spend,
    daily_budget,
    days_left_in_month,
    remaining_monthly_budget,
)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 1, 1), 31),
        (date(2026, 1, 31), 1),
        (date(2026, 2, 1), 28),
        (date(2028, 2, 1), 29),
        (date(2026, 4, 15), 16),
    ],
)
def test_days_left_includes_today(today, expected):
    """Hoje conta: a coleta de hoje ainda vai rodar.

    Sem isso, no dia 31 a divisão do orçamento seria por zero.
    """
    assert days_left_in_month(today) == expected


def test_remaining_budget_never_goes_negative():
    assert remaining_monthly_budget(0) == MONTHLY_RESULT_BUDGET
    assert remaining_monthly_budget(MONTHLY_RESULT_BUDGET) == 0
    assert remaining_monthly_budget(MONTHLY_RESULT_BUDGET + 500) == 0


def test_daily_budget_divides_what_is_left_by_the_days_left():
    """900 posts em 31 dias dá 29 por dia, abaixo do teto de 40."""
    assert daily_budget(0, date(2026, 1, 1)) == 900 // 31


def test_daily_budget_respects_the_cap():
    """Com muita sobra e poucos dias, o teto diário segura o gasto."""
    assert daily_budget(0, date(2026, 1, 25)) == MAX_DAILY_RESULTS


def test_daily_budget_is_zero_when_the_month_is_spent():
    assert daily_budget(MONTHLY_RESULT_BUDGET, date(2026, 1, 10)) == 0
    assert daily_budget(MONTHLY_RESULT_BUDGET + 100, date(2026, 1, 10)) == 0


def test_underspending_raises_the_following_days():
    """O gasto se auto-corrige sem ninguém reprogramar nada."""
    frugal = daily_budget(100, date(2026, 1, 15))
    wasteful = daily_budget(700, date(2026, 1, 15))
    assert frugal > wasteful


def test_a_month_of_daily_budgets_stays_inside_the_monthly_cap():
    """Simula janeiro inteiro gastando tudo que é liberado por dia."""
    spent = 0
    for day in range(1, 32):
        spent += daily_budget(spent, date(2026, 1, day))
    assert spent <= MONTHLY_RESULT_BUDGET


def test_can_spend_blocks_above_the_remaining_budget():
    assert can_spend(40, spent_this_month=0)
    assert can_spend(100, spent_this_month=800)
    assert not can_spend(101, spent_this_month=800)
    assert not can_spend(1, spent_this_month=MONTHLY_RESULT_BUDGET)


def test_can_spend_rejects_negative_amounts():
    """Gasto negativo seria uma forma silenciosa de devolver crédito."""
    assert not can_spend(-5, spent_this_month=0)


def test_can_spend_allows_a_zero_cost_call():
    assert can_spend(0, spent_this_month=MONTHLY_RESULT_BUDGET)
