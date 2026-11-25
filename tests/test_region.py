"""Região operacional (arquitetura seção 5)."""

import pytest

from prospector.core.region import RegionStatus, analyze_region, find_cities, haversine_km


def test_city_near_the_base():
    result = analyze_region("Carga saindo de Itaquaquecetuba")
    assert result.status is RegionStatus.INSIDE
    assert result.origin == "itaquaquecetuba"


def test_city_far_from_the_base():
    result = analyze_region("Rota para Ribeirão Preto")
    assert result.status is RegionStatus.OUTSIDE
    assert result.detail.startswith("longe da base: ribeirao preto")


@pytest.mark.parametrize("city", ["Sorocaba", "Sumaré", "Campinas", "Jundiaí", "Mogi das Cruzes"])
def test_operation_based_in_another_city_is_out_even_if_close(city):
    """O caminhão sai de Guarulhos e volta: não compensa operar a partir de lá."""
    result = analyze_region(f"Agregamos truck baú para operação em {city}")
    assert result.status is RegionStatus.OUTSIDE


def test_far_destination_is_fine_when_leaving_from_the_base():
    """Sair de Guarulhos e entregar em Campinas, voltando, serve."""
    assert analyze_region("Rota Guarulhos x Campinas").status is RegionStatus.INSIDE


@pytest.mark.parametrize("city", ["São Paulo", "Arujá", "Santo André", "Osasco", "Mairiporã"])
def test_greater_sao_paulo_close_to_guarulhos_is_in(city):
    assert analyze_region(f"Carga em {city}").status is RegionStatus.INSIDE


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


@pytest.mark.parametrize("text", ["Cadastre-se e receba cargas", "Precisa-se de truck baú"])
def test_hyphenated_se_is_not_sergipe(text):
    assert analyze_region(text).status is RegionStatus.UNKNOWN


@pytest.mark.parametrize("text", ["Entrega em Itabaiana/SE", "Entrega em Itabaiana - SE"])
def test_uf_after_slash_or_spaced_hyphen_still_counts(text):
    assert analyze_region(text).status is RegionStatus.OUTSIDE
