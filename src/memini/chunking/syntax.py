"""What a language description is made of: :class:`Spec`, :class:`Found`, :data:`Level`, and the
small helpers every classifier uses. :mod:`memini.chunking.languages` describes the languages."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from tree_sitter import Node

from memini.chunking.model import Language, SymbolKind

__all__ = ["Found", "Level", "Spec", "found", "name_of", "rewrap", "text_of"]

Level = Literal["top", "class", "local"]


@dataclass(frozen=True, slots=True)
class Found:
    """A definition introduced by a node."""

    name_path: tuple[str, ...]
    """The name, after any qualifier written in the definition itself: ``("Area",)``, or
    ``("Point", "Area")`` for a Go method, ``("A", "B")`` for Ruby ``class A::B``."""
    kind: SymbolKind
    span: Node
    """The node whose range is the definition, with its decorators and ``export`` keyword."""
    body: Node | None = None
    """The node whose named children are searched for nested definitions."""
    body_level: Level = "local"
    container: bool = False
    """The members of ``body`` become chunks of their own when the definition is too large."""
    end: Node | None = None
    """The node the definition ends with, when the grammar puts it beside ``span`` rather than in
    it (a Dart function body follows its signature); ``None`` when ``span`` holds it all."""


def found(
    name: str,
    kind: SymbolKind,
    span: Node,
    body: Node | None = None,
    *,
    container: Level | None = None,
    end: Node | None = None,
) -> list[Found]:
    """One definition named ``name``, or none if the name is empty (a syntax error).

    ``container`` is the level of the members of ``body`` when they can be chunks of their own.
    """
    if not name:
        return []
    if container is None:
        return [Found((name,), kind, span, body, end=end)]
    return [Found((name,), kind, span, body, body_level=container, container=True, end=end)]


@dataclass(frozen=True, slots=True)
class Spec:
    """How the chunker reads one language."""

    language: Language
    classify: Callable[[Node, Level], list[Found]]
    comments: frozenset[str] = frozenset({"comment"})
    """Node types of comments. A comment block right above a member belongs to it."""
    decorators: frozenset[str] = frozenset()
    """Node types that stand before a class member as siblings and belong to it."""
    anonymous_container: Callable[[Node], Node | None] = lambda _node: None
    """The body of a node that holds members without defining a symbol (Ruby ``class << self``,
    a Rust ``impl``). Its members belong to the enclosing scope, at ``class`` level."""
    transparent: Callable[[Node], Node | None] = lambda _node: None
    """The body of a node that only wraps declarations (a C header guard, ``extern "C" { }``).
    Its members belong to the enclosing scope, at the level of the node itself."""


def text_of(node: Node | None) -> str:
    """The source text of ``node``; ``""`` for none."""
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", "replace")


def name_of(node: Node) -> str:
    """The text of the ``name`` field of ``node``."""
    return text_of(node.child_by_field_name("name"))


def rewrap(definitions: list[Found], inner: Node, outer: Node) -> list[Found]:
    """Definitions of ``inner`` with ``outer`` (an export or a declare wrapper) as their span."""
    return [replace(f, span=outer) if f.span == inner else f for f in definitions]
