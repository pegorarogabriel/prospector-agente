"""Região operacional: o post fala de um lugar dentro do raio de atuação?

Portado de `app/services/location_service.py` do FacebookProspector. A lista de
cidades é a mesma; o que mudou é que a base e o raio entram por parâmetro em vez
de virem de `settings` — o `core` não lê configuração.

A operação sai de Guarulhos e volta para Guarulhos (`docs/arquitetura.md`
seção 1): a **origem** precisa ser perto da base, o destino pode ser longe
dentro de SP. Como a regra não sabe qual cidade do texto é a origem, ela usa
uma aproximação: o post passa se **alguma** cidade citada estiver no raio
("Guarulhos x Campinas" passa; "operação em Sorocaba" cai).

O raio é linha reta (haversine), não distância rodoviária. Na dúvida (nenhuma
cidade reconhecida, que é a maioria dos posts), o post segue para o juiz.
"""

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache

from prospector.core.text import normalize_text

#: Até onde pode ficar a origem da operação, em km da base. Era 100 no projeto
#: antigo, o que deixava passar Sorocaba e Sumaré: não compensa deslocar o
#: caminhão para operar lá.
MAX_DISTANCE_KM = 30.0

#: Cidade base padrão (precisa estar em `CITIES`).
HOME_BASE = "guarulhos"

# Cidade -> (lat, lon). O raio decide quem entra, não a lista: ela cobre a
# região em detalhe e as cidades grandes de fora, para reconhecer o que está fora.
CITIES: dict[str, tuple[float, float]] = {
    # --- Grande Sao Paulo ---
    "guarulhos": (-23.4543, -46.5337),
    "cumbica": (-23.4356, -46.4731),
    "sao paulo": (-23.5505, -46.6333),
    "osasco": (-23.5329, -46.7918),
    "barueri": (-23.5107, -46.8764),
    "alphaville": (-23.4980, -46.8520),
    "santo andre": (-23.6639, -46.5383),
    "sao bernardo do campo": (-23.6944, -46.5654),
    "sao bernardo": (-23.6944, -46.5654),
    "sao caetano do sul": (-23.6229, -46.5543),
    "sao caetano": (-23.6229, -46.5543),
    "diadema": (-23.6861, -46.6228),
    "maua": (-23.6677, -46.4613),
    "ribeirao pires": (-23.7107, -46.4133),
    "rio grande da serra": (-23.7440, -46.3980),
    "suzano": (-23.5426, -46.3110),
    "mogi das cruzes": (-23.5228, -46.1883),
    "poa": (-23.5286, -46.3450),
    "itaquaquecetuba": (-23.4863, -46.3487),
    "ferraz de vasconcelos": (-23.5411, -46.3690),
    "aruja": (-23.3968, -46.3210),
    "santa isabel": (-23.3159, -46.2226),
    "guararema": (-23.4148, -46.0335),
    "biritiba mirim": (-23.5730, -46.0380),
    "salesopolis": (-23.5314, -45.8460),
    "mairipora": (-23.3190, -46.5870),
    "franco da rocha": (-23.3286, -46.7267),
    "francisco morato": (-23.2816, -46.7452),
    "caieiras": (-23.3644, -46.7406),
    "cajamar": (-23.3556, -46.8769),
    "santana de parnaiba": (-23.4444, -46.9178),
    "pirapora do bom jesus": (-23.3958, -46.9986),
    "carapicuiba": (-23.5225, -46.8358),
    "itapevi": (-23.5489, -46.9346),
    "jandira": (-23.5278, -46.9022),
    "cotia": (-23.6039, -46.9190),
    "vargem grande paulista": (-23.5975, -47.0231),
    "taboao da serra": (-23.6014, -46.7917),
    "embu das artes": (-23.6489, -46.8524),
    "embu guacu": (-23.8320, -46.8117),
    "itapecerica da serra": (-23.7167, -46.8494),
    "juquitiba": (-23.9319, -47.0686),
    "sao lourenco da serra": (-23.8497, -46.9428),
    "maua sp": (-23.6677, -46.4613),
    # --- Jundiai / Atibaia / Braganca ---
    "jundiai": (-23.1857, -46.8978),
    "varzea paulista": (-23.2116, -46.8286),
    "campo limpo paulista": (-23.2072, -46.7833),
    "jarinu": (-23.1017, -46.7283),
    "itupeva": (-23.1528, -47.0578),
    "cabreuva": (-23.3067, -47.1330),
    "atibaia": (-23.1171, -46.5504),
    "bom jesus dos perdoes": (-23.1350, -46.4650),
    "nazare paulista": (-23.1811, -46.3967),
    "igarata": (-23.2044, -46.1550),
    "braganca paulista": (-22.9526, -46.5419),
    "itatiba": (-23.0059, -46.8386),
    # --- Campinas ---
    "campinas": (-22.9099, -47.0626),
    "valinhos": (-22.9705, -46.9958),
    "vinhedo": (-23.0299, -46.9750),
    "louveira": (-23.0870, -46.9503),
    "indaiatuba": (-23.0903, -47.2181),
    "hortolandia": (-22.8583, -47.2200),
    "sumare": (-22.8219, -47.2669),
    "paulinia": (-22.7614, -47.1543),
    "americana": (-22.7397, -47.3314),
    "santa barbara d oeste": (-22.7539, -47.4139),
    "limeira": (-22.5647, -47.4017),
    "piracicaba": (-22.7253, -47.6492),
    "mogi guacu": (-22.3717, -46.9422),
    "mogi mirim": (-22.4319, -46.9578),
    # --- Sorocaba / Itu ---
    "itu": (-23.2642, -47.2992),
    "salto": (-23.2006, -47.2870),
    "sorocaba": (-23.5015, -47.4526),
    "votorantim": (-23.5461, -47.4386),
    "sao roque": (-23.5290, -47.1358),
    "mairinque": (-23.5461, -47.1836),
    "aluminio": (-23.5341, -47.2578),
    "ibiuna": (-23.6564, -47.2225),
    "itapetininga": (-23.5915, -48.0531),
    "tatui": (-23.3556, -47.8569),
    # --- Vale do Paraiba ---
    "sao jose dos campos": (-23.1896, -45.8841),
    "jacarei": (-23.3053, -45.9658),
    "cacapava": (-23.1006, -45.7069),
    "taubate": (-23.0264, -45.5553),
    "pindamonhangaba": (-22.9239, -45.4614),
    "guaratingueta": (-22.8163, -45.1919),
    "aparecida": (-22.8478, -45.2306),
    "lorena": (-22.7306, -45.1247),
    "cruzeiro": (-22.5744, -44.9631),
    "santa branca": (-23.3961, -45.8828),
    "paraibuna": (-23.3861, -45.6628),
    "jambeiro": (-23.2547, -45.6931),
    # --- Baixada Santista ---
    "santos": (-23.9608, -46.3336),
    "sao vicente": (-23.9631, -46.3919),
    "guaruja": (-23.9931, -46.2564),
    "cubatao": (-23.8953, -46.4256),
    "praia grande": (-24.0058, -46.4028),
    "bertioga": (-23.8541, -46.1387),
    "itanhaem": (-24.1831, -46.7889),
    "peruibe": (-24.3200, -46.9989),
    # --- Litoral norte ---
    "caraguatatuba": (-23.6203, -45.4128),
    "sao sebastiao": (-23.7600, -45.4092),
    "ubatuba": (-23.4336, -45.0838),
    "ilhabela": (-23.7781, -45.3581),
    # --- Interior mais distante (fora do raio) ---
    "ribeirao preto": (-21.1775, -47.8103),
    "sao jose do rio preto": (-20.8113, -49.3758),
    "bauru": (-22.3147, -49.0606),
    "marilia": (-22.2139, -49.9456),
    "presidente prudente": (-22.1256, -51.3889),
    "aracatuba": (-21.2089, -50.4328),
    "franca": (-20.5386, -47.4006),
    "botucatu": (-22.8858, -48.4450),
    "avare": (-23.0989, -48.9258),
    "registro": (-24.4875, -47.8439),
    "sao carlos": (-22.0175, -47.8908),
    "araraquara": (-21.7947, -48.1756),
    "rio claro": (-22.4111, -47.5614),
    "jau": (-22.2964, -48.5578),
    "ourinhos": (-22.9797, -49.8697),
    "barretos": (-20.5575, -48.5678),
    # --- Outros estados / capitais ---
    "rio de janeiro": (-22.9068, -43.1729),
    "duque de caxias": (-22.7856, -43.3117),
    "nova iguacu": (-22.7592, -43.4511),
    "volta redonda": (-22.5231, -44.1042),
    "resende": (-22.4689, -44.4467),
    "belo horizonte": (-19.9167, -43.9345),
    "contagem": (-19.9320, -44.0539),
    "betim": (-19.9678, -44.1983),
    "juiz de fora": (-21.7642, -43.3503),
    "uberlandia": (-18.9186, -48.2772),
    "uberaba": (-19.7472, -47.9319),
    "montes claros": (-16.7350, -43.8617),
    "ipatinga": (-19.4683, -42.5367),
    "governador valadares": (-18.8511, -41.9494),
    "pouso alegre": (-22.2300, -45.9364),
    "extrema": (-22.8556, -46.3181),
    "curitiba": (-25.4284, -49.2733),
    "londrina": (-23.3045, -51.1696),
    "maringa": (-23.4253, -51.9386),
    "cascavel": (-24.9555, -53.4552),
    "joinville": (-26.3044, -48.8456),
    "blumenau": (-26.9194, -49.0661),
    "itajai": (-26.9078, -48.6619),
    "florianopolis": (-27.5954, -48.5480),
    "chapeco": (-27.1004, -52.6152),
    "porto alegre": (-30.0346, -51.2177),
    "caxias do sul": (-29.1678, -51.1794),
    "pelotas": (-31.7654, -52.3376),
    "santa maria": (-29.6842, -53.8069),
    "goiania": (-16.6869, -49.2648),
    "anapolis": (-16.3267, -48.9528),
    "brasilia": (-15.7939, -47.8828),
    "campo grande": (-20.4697, -54.6201),
    "cuiaba": (-15.6014, -56.0979),
    "rondonopolis": (-16.4708, -54.6356),
    "salvador": (-12.9777, -38.5016),
    "feira de santana": (-12.2664, -38.9663),
    "recife": (-8.0476, -34.8770),
    "fortaleza": (-3.7319, -38.5267),
    "natal": (-5.7945, -35.2110),
    "joao pessoa": (-7.1195, -34.8450),
    "maceio": (-9.6498, -35.7089),
    "aracaju": (-10.9472, -37.0731),
    "teresina": (-5.0892, -42.8019),
    "sao luis": (-2.5297, -44.3028),
    "belem": (-1.4558, -48.4902),
    "manaus": (-3.1190, -60.0217),
    "vitoria": (-20.3155, -40.3128),
    "serra": (-20.1286, -40.3078),
    "vila velha": (-20.3297, -40.2925),
    "cariacica": (-20.2639, -40.4164),
}

ALIASES: dict[str, str] = {
    "sjc": "sao jose dos campos",
    "sbc": "sao bernardo do campo",
    "s bernardo": "sao bernardo do campo",
    "grande sao paulo": "sao paulo",
    "capital paulista": "sao paulo",
    "zona leste": "sao paulo",
    "zona norte": "sao paulo",
    "zona sul": "sao paulo",
    "zona oeste": "sao paulo",
    "sao paulo capital": "sao paulo",
    "regiao do abc": "santo andre",
    "abcd": "santo andre",
    "baixada santista": "santos",
    "bh": "belo horizonte",
    "rio de janeiro rj": "rio de janeiro",
    "poa rs": "porto alegre",
    "porto alegre rs": "porto alegre",
}

# Cidades da lista que não são de SP: citar qualquer uma é estar fora de SP,
# mesmo sem UF escrita ("destino Curitiba", "entrega em BH").
CITIES_OUTSIDE_SP: frozenset[str] = frozenset(
    {
        "rio de janeiro",
        "duque de caxias",
        "nova iguacu",
        "volta redonda",
        "resende",
        "belo horizonte",
        "contagem",
        "betim",
        "juiz de fora",
        "uberlandia",
        "uberaba",
        "montes claros",
        "ipatinga",
        "governador valadares",
        "pouso alegre",
        "extrema",
        "curitiba",
        "londrina",
        "maringa",
        "cascavel",
        "joinville",
        "blumenau",
        "itajai",
        "florianopolis",
        "chapeco",
        "porto alegre",
        "caxias do sul",
        "pelotas",
        "santa maria",
        "goiania",
        "anapolis",
        "brasilia",
        "campo grande",
        "cuiaba",
        "rondonopolis",
        "salvador",
        "feira de santana",
        "recife",
        "fortaleza",
        "natal",
        "joao pessoa",
        "maceio",
        "aracaju",
        "teresina",
        "sao luis",
        "belem",
        "manaus",
        "vitoria",
        "serra",
        "vila velha",
        "cariacica",
    }
)


class RegionStatus(StrEnum):
    INSIDE = "inside"
    """Citou pelo menos uma cidade perto da base, e nada de outro estado."""

    OUTSIDE = "outside"
    """Viagem para outro estado, ou só cidades longe da base."""

    UNKNOWN = "unknown"
    """Nenhuma cidade reconhecida. É a maioria dos posts, e eles seguem adiante."""


@dataclass(frozen=True)
class RegionVerdict:
    status: RegionStatus
    detail: str | None = None
    origin: str | None = None
    """Primeira cidade citada, na ordem do texto."""

    destination: str | None = None
    """Segunda cidade citada."""


# Operação só em SP: outro estado por extenso derruba o post. "para" fica de
# fora de propósito: é preposição comum.
_STATE_OUTSIDE_SP = re.compile(
    r"\b(minas gerais|rio de janeiro|espirito santo|parana|santa catarina|"
    r"rio grande do sul|rio grande do norte|bahia|goias|mato grosso do sul|"
    r"mato grosso|distrito federal|brasilia|pernambuco|ceara|maranhao|paraiba|"
    r"alagoas|sergipe|piaui|tocantins|rondonia|acre|amazonas|amapa|roraima)\b"
)

# UF de outro estado logo depois de uma cidade: "santa vitoria/mg", "contagem - mg".
# O antigo aceitava qualquer separador, e "se" virava Sergipe em português comum:
# "caminhão, se você..." (vírgula) e "cadastre-se", "precisa-se" (hífen colado).
# Agora: barra, ou hífen com espaço de algum lado.
_UF_OUTSIDE_SP = re.compile(
    r"[a-z]{2,}(?:\s*/\s*|\s+-\s*|\s*-\s+)"
    r"(mg|rj|es|pr|sc|rs|rn|ba|go|ms|mt|df|pe|ce|ma|pb|al|se|pi|to|ro|ac|am|ap|rr)\b"
)


@cache
def _patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    """Um padrão por nome, os mais longos primeiro.

    Assim "sao jose dos campos" não é lido como "sao jose".
    """
    entries = list(ALIASES.items()) + [(city, city) for city in CITIES]
    entries.sort(key=lambda entry: len(entry[0]), reverse=True)
    return tuple(
        (re.compile(r"\b" + re.escape(name).replace(r"\ ", r"[\s\-/]+") + r"\b"), canonical)
        for name, canonical in entries
    )


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em linha reta entre dois pontos da Terra."""
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def find_cities(text: str) -> list[str]:
    """Cidades citadas no texto, na ordem em que aparecem."""
    normalized = normalize_text(text)
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pattern, canonical in _patterns():
        match = pattern.search(normalized)
        if match and canonical not in seen:
            seen.add(canonical)
            found.append((match.start(), canonical))
    found.sort()
    return [city for _, city in found]


def state_outside_sp(text: str) -> str | None:
    """O estado ou cidade fora de SP citado, quando há evidência clara."""
    normalized = normalize_text(text)
    for city in find_cities(normalized):
        if city in CITIES_OUTSIDE_SP:
            return city
    match = _STATE_OUTSIDE_SP.search(normalized)
    if match:
        return match.group(1)
    match = _UF_OUTSIDE_SP.search(normalized)
    if match:
        return match.group(1).upper()
    return None


def analyze_region(
    text: str,
    *,
    base: str = HOME_BASE,
    max_km: float = MAX_DISTANCE_KM,
) -> RegionVerdict:
    """Laudo de região do post.

    Outro estado tem prioridade: "Cabreúva/SP -> Santa Vitória/MG" sai, mesmo
    com Cabreúva dentro do raio, porque a viagem termina fora de SP.
    """
    base_key = normalize_text(base)
    if base_key not in CITIES:
        raise ValueError(f"cidade base desconhecida: {base!r}")
    base_lat, base_lon = CITIES[base_key]

    cities = find_cities(text)
    inside: list[tuple[str, int]] = []
    outside: list[tuple[str, int]] = []
    for city in cities:
        lat, lon = CITIES[city]
        distance = round(haversine_km(base_lat, base_lon, lat, lon))
        (inside if distance <= max_km else outside).append((city, distance))

    origin = cities[0] if cities else None
    destination = cities[1] if len(cities) > 1 else None

    elsewhere = state_outside_sp(text)
    if elsewhere:
        return RegionVerdict(
            RegionStatus.OUTSIDE, f"viagem fora de SP: {elsewhere}", origin, destination
        )
    if inside:
        detail = ", ".join(f"{city} ({km}km)" for city, km in inside)
        return RegionVerdict(RegionStatus.INSIDE, detail, origin, destination)
    if outside:
        detail = "longe da base: " + ", ".join(f"{city} ({km}km)" for city, km in outside)
        return RegionVerdict(RegionStatus.OUTSIDE, detail, origin, destination)
    return RegionVerdict(RegionStatus.UNKNOWN)
