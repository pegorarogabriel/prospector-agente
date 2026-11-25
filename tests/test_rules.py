"""Regras determinísticas (arquitetura seção 5).

Os casos vêm dos comentários do FacebookProspector, que registravam o post
real que motivou cada regra, e dos casos novos de posts só de imagem.
"""

from datetime import UTC, datetime, timedelta

import pytest

from prospector.core.post import Post
from prospector.core.region import RegionStatus
from prospector.core.rules import apply_rules, is_foreign_language, looks_like_driver_offering

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
FLYER = "https://scontent.xx.fbcdn.net/flyer.jpg"


def post(text: str, *, image_url: str | None = None, hours_old: float = 2) -> Post:
    return Post(
        id="p",
        text=text,
        url="https://facebook.com/p",
        posted_at=NOW - timedelta(hours=hours_old),
        image_url=image_url,
    )


def verdict(text: str, **kwargs):
    return apply_rules(post(text, **kwargs), now=NOW)


def test_good_offer_passes_with_vehicle_and_region():
    result = verdict("Precisamos de agregado truck baú, rota Guarulhos x Campinas")
    assert result.keep
    assert result.vehicle == "truck"
    assert result.body == "bau"
    assert result.region.status is RegionStatus.INSIDE
    assert (result.region.origin, result.region.destination) == ("guarulhos", "campinas")


# --- idade ----------------------------------------------------------------------


def test_old_post_is_dropped():
    result = verdict("Agrega truck baú", hours_old=72)
    assert not result.keep
    assert result.reason == "post velho: 72h"


def test_post_just_under_the_limit_stays():
    assert verdict("Agrega truck baú", hours_old=47).keep


# --- veículo e carroceria --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("Agregamos carreta sider", "carreta"),
        ("Procuro agregado VUC", "vuc"),
        ("Frete para fiorino", "fiorino"),
        ("Agrega 3/4 baú", "3/4"),
    ],
)
def test_incompatible_vehicle_is_dropped(text, label):
    result = verdict(text)
    assert not result.keep
    assert result.reason.startswith("veículo incompatível")
    assert label in result.reason


def test_incompatible_body_is_dropped():
    result = verdict("Agregamos truck graneleiro")
    assert not result.keep
    assert result.reason == "carroceria incompatível: graneleiro"


def test_mixed_offer_is_kept_and_flagged():
    """ "Agrega truck baú e carreta sider": cita o que serve e o que não serve."""
    result = verdict("Agregamos truck baú e carreta sider, cargas em Guarulhos")
    assert result.keep
    assert result.mixed


def test_ambiguous_toco_alone_is_not_a_vehicle():
    """ "me toco" em espanhol chegou na fila rotulado vehicle=toco."""
    result = verdict("Precisamos de agregado toco")
    assert result.vehicle is None


def test_toco_with_freight_context_is_a_vehicle():
    assert verdict("Agregamos toco baú para cargas em Guarulhos").vehicle == "toco"


# --- texto, imagem e idioma --------------------------------------------------------


def test_no_text_and_no_image_is_dropped():
    result = verdict("   ")
    assert not result.keep
    assert result.reason == "sem texto nem imagem"


def test_image_only_post_goes_to_the_judge():
    """Muita oportunidade vem como flyer: sem texto, quem decide é o juiz."""
    assert verdict("", image_url=FLYER).keep


def test_text_without_freight_signal_but_with_image_goes_to_the_judge():
    """ "Chama no zap" + um flyer: o conteúdo está na imagem."""
    assert verdict("Chama no zap!", image_url=FLYER).keep


def test_text_without_freight_signal_and_no_image_is_dropped():
    """Vela aromática, desfile e futebol enchiam a fila do antigo."""
    result = verdict("Promoção de velas aromáticas, aproveite")
    assert not result.keep
    assert result.reason == "sem sinal de frete"


def test_spanish_agregado_is_dropped():
    """ "agregado" em espanhol é "adicionado": todos os 10 posts em espanhol entraram por ela."""
    result = verdict("Necesito agregado para el trabajo con mi hijo, gracias a Dios")
    assert not result.keep
    assert result.reason == "não está em português"


def test_portuguese_is_never_taken_for_spanish():
    assert not is_foreign_language("Preciso de agregado truck, tenho carga, chamar no zap")


# --- venda e serviço -----------------------------------------------------------------


def test_truck_for_sale_is_dropped():
    result = verdict("Vendo caminhão toco 2015, único dono, km rodados baixo")
    assert not result.keep
    assert result.reason.startswith("não é agregamento")


def test_truck_word_is_a_freight_signal():
    """O antigo não reconhecia "caminhao" (só "caminhoes") como sinal de frete."""
    assert verdict("Preciso de caminhão para carga em Guarulhos").keep


def test_vehicle_ad_is_dropped_even_mentioning_agregamento():
    """O Chevrolet Montana à venda escapava porque citava agregamento no fim.

    Aqui com um truck, que passa na regra de veículo e só cai pela do anúncio.
    """
    result = verdict("Truck baú 2019 único dono, quilômetros rodados 40 mil. Serve p/ agregamento")
    assert not result.keep
    assert "anuncio de veiculo" in result.reason


def test_vehicle_protection_is_dropped():
    result = verdict("Proteção veicular para seu caminhão, assistência 24h e guincho")
    assert not result.keep
    assert "protecao veicular" in result.reason


def test_carrier_ad_with_agregamento_and_service_words_is_kept():
    """Anúncio de transportadora costuma citar agregamento e serviço juntos."""
    assert verdict("Transportadora oferece serviços e está agregando truck baú").keep


# --- região ----------------------------------------------------------------------------


def test_trip_to_another_state_is_dropped():
    result = verdict("Frete truck baú Guarulhos para Curitiba")
    assert not result.keep
    assert result.reason == "região: viagem fora de SP: curitiba"


def test_unknown_region_stays():
    result = verdict("Precisamos de agregado truck baú")
    assert result.keep
    assert result.region.status is RegionStatus.UNKNOWN


# --- motorista se oferecendo (sinal, não filtro) ------------------------------------------


def test_driver_offering_is_flagged_but_kept():
    result = verdict("Tenho truck baú disponível para fretes")
    assert result.keep
    assert result.driver_offering


def test_company_offering_cargo_is_not_a_driver():
    """ "Está vazio? Temos carga pra você": oferta de carga tem prioridade."""
    assert not looks_like_driver_offering("Está vazio? Temos carga para truck")
