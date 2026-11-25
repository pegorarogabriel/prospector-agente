"""Região operacional (arquitetura seção 5)."""

import pytest

from prospector.core.region import RegionStatus, analyze_region, find_cities, haversine_km


def test_city_inside_radius():
    result = analyze_region("Carga saindo de Jundiaí")
    assert result.status is RegionStatus.INSIDE
    assert result.origin == "jundiai"


def test_city_outside_radius():
    result = analyze_region("Rota para Ribeirão Preto")
    assert result.status is RegionStatus.OUTSIDE
    assert result.detail.startswith("fora do raio: ribeirao preto")


def test_another_state_wins_over_a_city_inside():
    """A viagem termina fora de SP, mesmo saindo de dentro do raio."""
    result = analyze_region("Frete Guarulhos para Belo Horizonte")
    assert result.status is RegionStatus.OUTSIDE
    assert result.detail == "viagem fora de SP: belo horizonte"


def test_state_written_in_full():
    assert analyze_region("Carga para Minas Gerais").status is RegionStatus.OUTSIDE


def test_uf_after_city():
    result = analyze_region("Entrega em Pouso Grande - MG")
    assert result.status is RegionStatus.OUTSIDE
    assert result.detail == "viagem fora de SP: MG"


def test_no_city_is_unknown():
    assert analyze_region("Preciso de truck baú").status is RegionStatus.UNKNOWN


def test_longest_name_wins():
    assert find_cities("Base em São José dos Campos") == ["sao jose dos campos"]


def test_aliases():
    assert find_cities("Atendemos SJC e ABCD") == ["sao jose dos campos", "santo andre"]


def test_radius_is_a_parameter():
    assert analyze_region("Carga em Campinas", max_km=50).status is RegionStatus.OUTSIDE


def test_unknown_base_fails_loudly():
    with pytest.raises(ValueError):
        analyze_region("Campinas", base="Atlântida")


def test_haversine_guarulhos_to_campinas():
    assert haversine_km(-23.4543, -46.5337, -22.9099, -47.0626) == pytest.approx(81, abs=1)


def test_portuguese_se_after_a_comma_is_not_sergipe():
    """ "...caminhão, se você precisa" não é Sergipe. O antigo caía nessa."""
    assert analyze_region("Agregamos caminhão, se você tem truck chama").status is (
        RegionStatus.UNKNOWN
    )
