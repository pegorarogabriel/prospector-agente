"""Orçamento do Apify: uma trava em código, não uma sugestão ao LLM.

Ver `docs/arquitetura.md` seção 7. O plano Free do Apify dá US$ 5/mês de crédito,
≈ 1.000 posts; o crédito não acumula e, quando acaba, o Apify bloqueia. O agente
decide como distribuir o orçamento, nunca quanto gastar acima do teto.
"""

import calendar
from datetime import date

#: Teto mensal de resultados, com folga sobre os ~1.000 do crédito.
MONTHLY_RESULT_BUDGET = 900

#: Teto do orçamento diário, mesmo quando a conta daria mais.
MAX_DAILY_RESULTS = 40

#: Semanas seguidas sem resposta minha a partir das quais o orçamento cai pela
#: metade (seção 4): não gastar crédito do Apify sem ninguém olhando.
SILENT_WEEKS_BUDGET_CUT = 4


def days_left_in_month(today: date) -> int:
    """Dias que ainda restam no mês, contando hoje.

    Conta hoje porque a coleta de hoje ainda vai acontecer — sem isso, no último
    dia do mês a divisão seria por zero.
    """
    _, last_day = calendar.monthrange(today.year, today.month)
    return last_day - today.day + 1


def remaining_monthly_budget(
    spent_this_month: int,
    *,
    monthly_budget: int = MONTHLY_RESULT_BUDGET,
) -> int:
    """Quanto ainda sobra do teto do mês. Nunca negativo."""
    return max(monthly_budget - spent_this_month, 0)


def daily_budget(
    spent_this_month: int,
    today: date,
    *,
    monthly_budget: int = MONTHLY_RESULT_BUDGET,
    cap: int = MAX_DAILY_RESULTS,
    silent_weeks: int = 0,
) -> int:
    """Quantos resultados a coleta de hoje pode gastar.

    É o que resta do mês dividido pelos dias que faltam, limitado pelo teto
    diário. Isso faz o gasto se auto-corrigir: um dia que gasta menos aumenta a
    sobra dos outros, e um mês que começou esbanjando aperta no fim sozinho,
    sem ninguém reprogramar nada.

    Divisão inteira, porque meio post não existe — e arredondar para baixo erra
    para o lado seguro.

    Depois de `SILENT_WEEKS_BUDGET_CUT` semanas seguidas sem resposta minha, o
    resultado cai pela metade. A primeira resposta desfaz o corte sozinha.
    """
    remaining = remaining_monthly_budget(spent_this_month, monthly_budget=monthly_budget)
    budget = min(remaining // days_left_in_month(today), cap)
    if silent_weeks >= SILENT_WEEKS_BUDGET_CUT:
        budget //= 2
    return budget


def can_spend(
    amount: int,
    spent_this_month: int,
    *,
    monthly_budget: int = MONTHLY_RESULT_BUDGET,
) -> bool:
    """A trava verificada ANTES de toda chamada ao Apify.

    Os testes de receita do agente semanal (`testar_receita`) passam por aqui
    também: saem do mesmo orçamento que a coleta diária.
    """
    if amount < 0:
        return False
    return amount <= remaining_monthly_budget(spent_this_month, monthly_budget=monthly_budget)
