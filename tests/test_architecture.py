"""Protege as regras de arquitetura descritas em `docs/arquitetura.md` seção 8.

A regra de dependência é `integrations -> flows -> core`. O `core` não importa
nada de fora da biblioteca padrão, e nenhuma camada importa outra que esteja
mais para fora que ela.

Estes testes leem o código com `ast` em vez de importar os módulos, então
funcionam mesmo em camadas que ainda não existem — eles passam enquanto as
pastas estão vazias e começam a valer sozinhos conforme o código entra.
"""

import ast
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "prospector"

LAYERS = ("core", "flows", "agent", "integrations")

# O que cada camada NÃO pode importar. Ver a regra de dependência na arquitetura:
# `core` é o centro e não conhece ninguém; `integrations` é a borda e pode tudo.
FORBIDDEN_IMPORTS = {
    "core": ("flows", "agent", "integrations"),
    "flows": ("agent", "integrations"),
    "agent": ("integrations",),
    "integrations": (),
}


def _package_of(module: Path) -> str:
    """Pacote ao qual o módulo pertence, em notação pontuada.

    `src/prospector/core/bandit.py` e `src/prospector/core/__init__.py` ambos
    pertencem a `prospector.core`.
    """
    parts = module.relative_to(PACKAGE_ROOT.parent).parts
    return ".".join(parts[:-1])


def _resolve_relative(package: str, level: int, module: str | None) -> str:
    """Converte um import relativo no nome absoluto equivalente."""
    base = package.rsplit(".", level - 1)[0] if level > 1 else package
    return f"{base}.{module}" if module else base


def _imports_of(module: Path) -> set[str]:
    """Nomes absolutos de tudo que o módulo importa, relativos já resolvidos."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    package = _package_of(module)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add(_resolve_relative(package, node.level, node.module))
            elif node.module:
                names.add(node.module)
    return names


def _modules_in(layer: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / layer).rglob("*.py"))


def test_all_layers_exist():
    """As quatro camadas da arquitetura existem como pacotes."""
    for layer in LAYERS:
        assert (PACKAGE_ROOT / layer / "__init__.py").is_file(), f"camada `{layer}` não é um pacote"


def test_core_imports_only_stdlib():
    """`core/` não importa nada externo (nem LangChain, nem SQLAlchemy, nem requests)."""
    allowed = sys.stdlib_module_names | {"prospector"}
    offenders: dict[str, list[str]] = {}
    for module in _modules_in("core"):
        external = sorted({name.split(".")[0] for name in _imports_of(module)} - allowed)
        if external:
            offenders[module.name] = external
    assert not offenders, f"core/ importou dependência externa: {offenders}"


def test_layers_respect_dependency_rule():
    """Nenhuma camada importa outra mais externa que ela."""
    offenders: dict[str, list[str]] = {}
    for layer, forbidden in FORBIDDEN_IMPORTS.items():
        banned = tuple(f"prospector.{other}" for other in forbidden)
        for module in _modules_in(layer):
            crossing = sorted(
                name
                for name in _imports_of(module)
                if any(name == b or name.startswith(f"{b}.") for b in banned)
            )
            if crossing:
                offenders[f"{layer}/{module.name}"] = crossing
    assert not offenders, f"import cruzando a regra de dependência: {offenders}"


def test_package_is_importable():
    """O pacote está instalado e expõe a versão."""
    import prospector

    assert prospector.__version__
