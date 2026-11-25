"""Normalização de texto: a base de todo casamento de regra.

Portado de `app/processing/text_utils.py` do FacebookProspector.
"""

import re
import unicodedata


def strip_accents(text: str | None) -> str:
    """Remove acentos: "caminhão" vira "caminhao"."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def normalize_text(text: str | None) -> str:
    """Minúsculo, sem acento e com espaços colapsados."""
    return re.sub(r"\s+", " ", strip_accents(text).lower()).strip()


def collapse(text: str | None) -> str:
    """Espaços colapsados, mantendo maiúsculas e acentos."""
    return re.sub(r"\s+", " ", (text or "").strip())
