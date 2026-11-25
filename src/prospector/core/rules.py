"""Triagem, camada 1: regras de negócio determinísticas.

Ver `docs/arquitetura.md` seção 5. Portado de `app/processing/filters.py` e
`app/processing/age_filter.py` do FacebookProspector; as tabelas de padrões são
as mesmas, com os comentários de onde cada uma veio.

Isto não é território de IA. Se a operação não roda VUC, VUC é descartado por
regra fixa, e não por um modelo que "aprendeu" que VUC costuma ser ruim. Roda
antes do juiz de propósito: elimina o obviamente inválido antes de gastar LLM.

O que mudou em relação ao antigo: **post só de imagem não é descartado por
falta de texto.** Muita oportunidade vem como flyer ou print do WhatsApp; sem
texto, as regras não têm o que ler, e quem decide é o juiz, que vê a imagem.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from prospector.core.post import Post
from prospector.core.region import (
    HOME_BASE,
    MAX_DISTANCE_KM,
    RegionStatus,
    RegionVerdict,
    analyze_region,
)
from prospector.core.text import normalize_text, strip_accents

#: Idade máxima do post. Oportunidade de frete envelhece rápido.
MAX_POST_AGE = timedelta(hours=48)

# --- veiculos ---
VEHICLE_OK: dict[str, str] = {
    "toco": r"\btocos?\b",
    "truck": r"\btrucks?\b|\btrucados?\b",
}

# "toco" e palavra comum em portugues e espanhol ("me toco", "no toco", "toco
# madera", "Turma do Toco"). Medido na primeira coleta real: 6 posts em espanhol
# sobre bebe, aniversario e pet chegaram na fila rotulados vehicle=toco.
AMBIGUOUS_VEHICLE: set[str] = {"toco"}

VEHICLE_BAD: dict[str, str] = {
    "carreta": r"\bcarretas?\b|\bcavalos?\s+mecanicos?\b",
    "bitrem": r"\bbitrens?\b|\bbi-?trem\b",
    "rodotrem": r"\brodotrens?\b|\brodo-?trem\b",
    "vanderleia": r"\bvanderleias?\b",
    "romeu e julieta": r"\bromeu\s+e\s+julieta\b|\bjulieta\b",
    "bitruck": r"\bbi-?trucks?\b",
    "vuc": r"\bvucs?\b",
    "fiorino": r"\bfiorinos?\b",
    "van": r"\bvans?\b|\bkombis?\b|\bducato\b|\bsprinter\b|\bmaster\b|\bfurgoes?\b|\bfurgao\b",
    "moto": r"\bmotos?\b|\bmotoboys?\b|\bmotofrete\b|\bmotociclistas?\b",
    "3/4": r"(?<!\d)3\s*/\s*4(?!\d)|\btres\s+quartos\b",
    "utilitario leve": r"\butilitarios?\b|\bsaveiros?\b|\bstradas?\b|\bmontanas?\b|"
    r"\bhilux\b|\bs10\b|\bpick-?ups?\b|\bcarros?\s+de\s+passeio\b",
    "leve": r"\bhr\b|\bbongos?\b|\bdaily\b|\baccelo\b|\bvans?\s+bau\b",
}

# --- carrocerias ---
BODY_OK: dict[str, str] = {
    "bau": r"\bbaus?\b|\bbau\s+seco\b|\bbau\s+liso\b",
}

BODY_BAD: dict[str, str] = {
    "sider": r"\bsiders?\b|\bside-?r\b|\blonados?\b|\blonas?\s+corredicas?\b",
    "graneleiro": r"\bgraneleiros?\b|\bgrade\s+baixa\b",
    "cacamba": r"\bcacambas?\b|\bbasculantes?\b",
    "prancha": r"\bpranchas?\b",
    "tanque": r"\btanques?\b|\bbitanque\b",
    "silo": r"\bsilos?\b",
    "frigorifico": r"\bfrigorificos?\b|\bcamaras?\s+fria\b|\brefrigerados?\b",
    "porta container": r"\bporta[-\s]?containers?\b|\bporta[-\s]?conteiner\b",
    "carga seca": r"\bcarga\s+seca\b|\bcarroceria\s+aberta\b",
}

# agregamento em qualquer forma: agrega, agrego, agregado, agregamento, agregamos
AGREG_SIGNAL = r"\bagreg\w+\b"

# --- sinal de relevancia ---
# Um post sem NENHUM destes nao e avaliavel pela operacao: nao fala de frete, de
# caminhao nem de agregamento. Sem esta regra a fila enche de post de vela
# aromatica, desfile e futebol - medido na primeira coleta real: 14 dos 37 que
# chegaram na fila nao tinham sinal nenhum, e 6 estavam com texto vazio.
#
# Isto e regra fixa, nao ML: "o post nao fala de frete" nao e uma opiniao que um
# modelo aprende com o tempo, e uma condicao de contorno da operacao.
RELEVANCE_EXTRA: dict[str, str] = {
    "agregamento": AGREG_SIGNAL,
    "frete": r"\bfretes?\b|\bfretista\w*\b|\bfretad\w+\b",
    "carga": r"\bcargas?\b|\bcarregament\w+\b|\bembarcador\w*\b|\bembarques?\b|"
    r"\btoneladas?\b|\bpalletes?\b|\bpaletes?\b|\bpallets?\b",
    # O antigo tinha `caminho(?:es|ao)`, que casa "caminhoes" mas não "caminhao".
    "caminhao": r"\bcaminh(?:oes|ao)\b|\bcaminhoneir\w+\b|\bcarreteir\w+\b",
    "transporte": r"\btransportadoras?\b|\btransportes?\b|\btransportad\w+\b|"
    r"\blogistica\b|\bfrota\b",
    "carroceria": r"\bcarrocerias?\b|\beixos?\b",
    "documento": r"\bcte\b|\bmdfe?\b|\brntrc\b|\bantt\b",
    "operacao": r"\brotas?\b|\bviage(?:m|ns)\b|\bmotoristas?\b|\bdiarias?\b",
}

# Mesmo problema do "toco", e pior: "agregado"/"agrega" em espanhol significa
# "adicionado". Foi por essa unica palavra que TODOS os 10 posts em espanhol da
# primeira coleta entraram na fila - e e por ela que o proprio Actor os coletou,
# ja que a busca "agregado truck" casa com prosa espanhola qualquer.
#
# Nao da pra remover: e o sinal principal do negocio. Mas sozinha ela nao basta -
# um post de agregamento de verdade cita veiculo, carroceria, carga ou frete.
AMBIGUOUS_SIGNAL: set[str] = {"agregamento"}

# --- anuncio que nunca e oportunidade de agregamento ---
# Quem vende, financia, aluga, da curso ou conserta nao esta oferecendo
# agregamento. A camada 2 ja reconhecia parte disso, mas so marcava
# is_opportunity=False - o post continuava ocupando a fila.
NOT_OPPORTUNITY: dict[str, str] = {
    "venda": r"\bvende-?se\b|\bvendo\b|\bvenda\b|\bseminovos?\b|\bzero\s+km\b",
    "financiamento": r"\bfinanci\w+\b|\bconsorcio\b|\bparcelas?\s+de\b|\bentrada\s+de\b",
    "aluguel": r"\balugo\b|\baluga-?se\b|\blocacao\b|\bloca-?se\b",
    "curso": r"\bcursos?\b|\btreinamentos?\b|\bmentorias?\b|\bpalestras?\b",
    "anuncio de veiculo": r"\b(?:quilometros|km)\s+rodados\b|\bunico\s+dono\b",
    "oficina": r"\bconsertos?\b|\bmanutencao\b|\boficinas?\b|\bfunilaria\b",
    # Empresa de protecao veicular / seguro / associacao / rastreamento nao esta
    # oferecendo agregamento - esta vendendo servico pro caminhoneiro. Medido nas
    # recusas reais: "for truck", "protecao de caminhao", "averbacao/documentacao".
    "protecao veicular": r"\bprotecao\s+veicular\b|\bprotecao\s+de\s+caminho\w+\b|"
    r"\bassociacao\b|\brastreador\w*\b|\brastreament\w+\b|"
    r"\bfor\s+truck\b|\bassistencia\s+24\b|\bguincho\b|\bapolice\b|"
    r"\bseguradora\b",
    "prestacao de servico": r"\bprestando\s+servicos?\b|\bpresta\w+\s+de\s+servicos?\b|"
    r"\bpresto\s+servicos?\b|\boferec\w+\s+servicos?\b|"
    r"\baverbacao\b|\bgerenciamento\s+de\s+risco\b",
    # servico administrativo/contabil pro caminhoneiro - nao e oferta de frete
    "servico administrativo": r"\bnotas?\s+fisca(?:l|is)\b|\bemitir\s+nota\b|"
    r"\bemissao\s+de\s+nota\b|\bcontador\w*\b|\bcontabilidade\b",
    # plataforma de cotacao / anuncio de marketplace de frete - nao e a oferta em si
    "plataforma de cotacao": r"\borcamentos?\b|\bcotac(?:ao|oes)\b|\bcadastre-se\b|"
    r"\bcadastro\s+liberado\b|\banunci\w+\s+gratis\b|"
    r"\breceba\s+ate\s+\d+\b|\bbaixe\s+o\s+app\b",
}


# "quilometros rodados" e "unico dono" sao marcadores estruturais de anuncio
# do Marketplace - nunca aparecem numa oferta de agregamento. Estes filtram
# mesmo quando o post tambem cita agregamento: medido no post do Chevrolet
# Montana a venda, que escapava porque a palavra aparecia solta no fim do texto.
HARD_NOT_OPPORTUNITY: set[str] = {"anuncio de veiculo"}


# --- idioma ---
# Metade das recusas reais (29 de 59) foram "nao esta em portugues": posts em
# espanhol/ingles que vazam porque a busca "agregado" casa com "adicionado" em
# espanhol. Detectar idioma e' regra fixa (uma condicao objetiva), nao ML.
#
# Trabalha no texto ORIGINAL (com acento) - normalize_text apagaria ¡¿ñ e os
# acentos que separam "tambien" (es) de "tambem" (pt). So marca estrangeiro com
# evidencia forte E nenhum sinal de portugues, pra nunca descartar post em pt.
# Palavras funcionais exclusivas de cada idioma (comparadas sem acento, porque o
# texto do Actor chega com acento corrompido). Os artigos/preposicoes espanhois
# (el, la, los, del, un, y, en) sao o sinal mais forte e sobrevivem a corrupcao.
_ES: set[str] = set(
    (
        "el la los las del al un una unos unas y en lo mi mis muy pero este ese esa esto "
        "eso ella ellos ellas nosotros usted ustedes nuestro nuestra conmigo contigo su sus "
        "son soy eres somos estan estas tengo tienes tiene tenemos tienen quiero quieres "
        "quiere puedo puedes puede hay hijo hija tambien siempre entonces mientras aunque "
        "mismo mucho mucha muchos muchas ahora manana hasta despues chofer dinero trabajo "
        "hola gracias autobus nieto encargos senor senora dios muerte vida amor"
    ).split()
)
_EN: set[str] = set(
    (
        "the and you your with we are this that our from have will they their what when "
        "about more business service available contact driver drivers trucker truckers "
        "for now here there people money week day"
    ).split()
)
# So palavras INEQUIVOCAS de portugues. Nada que o espanhol compartilhe - em
# especial "agrega/agregado/agregar", que sao espanhol ("adicionado") e foram o
# proprio motivo dos posts ES entrarem (a busca "agregado" casa prosa espanhola).
_PT: set[str] = set(
    (
        "nao voce voces sao entao muito caminhao caminhoes obrigado procuro vaga vagas "
        "preciso tenho estou fazemos rota rotas frete fretes agregamento bau chamar zap"
    ).split()
)
_ES_MARKS = re.compile(r"[¡¿ñ]")
_WORD = re.compile(r"[a-z]+")

# --- direcao da oferta (sinal, nao filtro) ---
# O negocio e' AGREGAR os caminhoes da operacao a quem tem carga. Entao interessa
# EMPRESA oferecendo carga/agregamento - nao outro motorista se oferecendo. Isto
# NAO filtra (decisao do Gabriel: so marcar): a fila mostra uma etiqueta pra ele
# pular rapido quem esta se oferecendo.
_LOOKING_FOR_WORK = re.compile(
    r"\bprocuro\s+(?:frete|carga|agrega\w*|trabalho|rota|viage|servico|carreg)"
    r"|disponivel\s+para\s+(?:frete|viage|carga|carreg|trabalh|rota)"
    r"|para\s+fretes?\s+e\s+viage"
    r"|\btenho\s+(?:um\s+|uma\s+)?(?:truck|toco|caminhao|carreta|bitruck|bau|veiculo|van|fiorino)\b"
    r"|\bestou\s+(?:vazio|disponivel|parado|a\s+disposicao)\b"
    r"|\baceito\s+fretes?\b|\bfaco\s+fretes?\b|\bfaco\s+viage"
    r"|\bmotorista\s+(?:disponivel|a\s+disposicao)"
    r"|\bdisponivel\s+(?:em|na|no|a\s+partir)\b"
    r"|\bvolta(?:ndo)?\s+(?:para|pra|vazi[ao]|de)"
    r"|\bvazi[ao]\b"
)

# Se o texto claramente OFERECE carga/agregamento, nao e "se oferecendo" - mesmo
# citando "vazio" ("esta vazio? temos carga pra voce"). Oferta tem prioridade.
_OFFERS_CARGO = re.compile(
    r"estamos\s+agregando|agregamos\b|temos\s+carga|carga\s+para|cargas?\s+disponiveis"
    r"|vagas?\s+para|contratamos|contrata-se|precisa-?se|precisamos\s+de"
    r"|ampliando\s+(?:a\s+)?frota|estamos\s+com\s+carga|oferecemos\s+(?:carga|frete)"
)


@dataclass(frozen=True)
class RuleVerdict:
    """O laudo das regras sobre um post."""

    keep: bool
    reason: str | None = None
    """Por que caiu. Vai para `TriageResult.reason` quando o post é descartado aqui."""

    vehicle: str | None = None
    body: str | None = None
    mixed: bool = False
    """Cita algo compatível e algo incompatível na mesma oferta ("truck baú e carreta sider")."""

    driver_offering: bool = False
    """Parece motorista se oferecendo, e não empresa oferecendo carga. Só um sinal ao juiz."""

    region: RegionVerdict = field(default_factory=lambda: RegionVerdict(RegionStatus.UNKNOWN))


def _hits(text: str, table: dict[str, str]) -> list[str]:
    return [label for label, pattern in table.items() if re.search(pattern, text)]


def _relevance_regex() -> re.Pattern[str]:
    """Veículo e carroceria já são sinal de frete: reaproveita as tabelas acima.

    Os termos ambíguos ficam de fora: "toco" e "agregado" sozinhos não provam nada.
    """
    patterns = [p for label, p in RELEVANCE_EXTRA.items() if label not in AMBIGUOUS_SIGNAL]
    patterns += [p for label, p in VEHICLE_OK.items() if label not in AMBIGUOUS_VEHICLE]
    patterns += [p for label, p in VEHICLE_BAD.items() if label not in AMBIGUOUS_VEHICLE]
    patterns += list(BODY_OK.values()) + list(BODY_BAD.values())
    return re.compile("|".join(patterns))


RELEVANCE = _relevance_regex()


def is_foreign_language(text: str) -> bool:
    """O post é claramente espanhol ou inglês, sem pista de português?

    Trabalha no texto original: normalizar apagaria ¡¿ñ. Exige evidência forte
    e proporção sobre o português, para nunca descartar post em português.
    """
    original = (text or "").lower()
    tokens = _WORD.findall(strip_accents(original))
    spanish = sum(1 for token in tokens if token in _ES)
    english = sum(1 for token in tokens if token in _EN)
    portuguese = sum(1 for token in tokens if token in _PT)
    strength = spanish + english + 2 * len(_ES_MARKS.findall(original))
    return strength >= 3 and spanish + english > 2 * portuguese


def looks_like_driver_offering(text: str) -> bool:
    """Motorista ou autônomo se oferecendo, e não empresa oferecendo carga."""
    normalized = normalize_text(text)
    if _OFFERS_CARGO.search(normalized):
        return False
    return bool(_LOOKING_FOR_WORK.search(normalized))


def apply_rules(
    post: Post,
    *,
    now: datetime,
    base: str = HOME_BASE,
    max_km: float = MAX_DISTANCE_KM,
    max_age: timedelta = MAX_POST_AGE,
) -> RuleVerdict:
    """O laudo das regras, na ordem do antigo: o motivo mais específico ganha.

    1. idade (post velho não é oportunidade);
    2. veículo e carroceria incompatíveis;
    3. sem texto e sem imagem;
    4. idioma estrangeiro;
    5. sem sinal de frete — só quando há texto: post só de imagem segue;
    6. venda, financiamento, serviço;
    7. região fora do raio.
    """
    if post.age(now) > max_age:
        hours = int(post.age(now).total_seconds() // 3600)
        return RuleVerdict(keep=False, reason=f"post velho: {hours}h")

    text = normalize_text(post.text)
    has_text = bool(text)
    vehicle_ok = _hits(text, VEHICLE_OK)
    vehicle_bad = _hits(text, VEHICLE_BAD)
    body_ok = _hits(text, BODY_OK)
    body_bad = _hits(text, BODY_BAD)
    corroborated = bool(RELEVANCE.search(text))

    # "toco" sem corroboração não é veículo identificado, é coincidência de idioma.
    vehicle = next(
        (label for label in vehicle_ok if not (label in AMBIGUOUS_VEHICLE and not corroborated)),
        None,
    )
    body = body_ok[0] if body_ok else None
    region = (
        analyze_region(post.text, base=base, max_km=max_km)
        if has_text
        else RegionVerdict(RegionStatus.UNKNOWN)
    )

    def drop(reason: str) -> RuleVerdict:
        return RuleVerdict(keep=False, reason=reason, vehicle=vehicle, body=body, region=region)

    if vehicle_bad and not vehicle_ok:
        return drop("veículo incompatível: " + ", ".join(vehicle_bad))
    if body_bad and not body_ok:
        return drop("carroceria incompatível: " + ", ".join(body_bad))
    if not has_text and not post.image_url:
        return drop("sem texto nem imagem")
    if has_text and is_foreign_language(post.text):
        return drop("não está em português")
    if has_text and not corroborated and not post.image_url:
        return drop("sem sinal de frete")

    # Venda, financiamento, curso... Se o post também fala de agregamento, não
    # descarta: anúncio de transportadora costuma citar as duas coisas.
    sale = _hits(text, NOT_OPPORTUNITY)
    hard = [label for label in sale if label in HARD_NOT_OPPORTUNITY]
    if hard or (sale and not re.search(AGREG_SIGNAL, text)):
        return drop("não é agregamento: " + ", ".join(sale))

    if region.status is RegionStatus.OUTSIDE:
        return drop("região: " + (region.detail or "fora do raio"))

    return RuleVerdict(
        keep=True,
        vehicle=vehicle,
        body=body,
        mixed=bool((vehicle_bad and vehicle_ok) or (body_bad and body_ok)),
        driver_offering=has_text and looks_like_driver_offering(post.text),
        region=region,
    )
