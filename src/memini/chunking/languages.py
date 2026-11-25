"""Languages: detection by file extension, grammars, and what counts as a definition in each.

A language is described by a :class:`Spec`. Its ``classify`` function looks at one node of the
syntax tree and returns the definitions the node introduces, as :class:`Found` values. The chunker
calls it on the members of a file or of a class body, and on every node below them, with the
:data:`Level` the node sits at:

- ``top``: a member of the file, or of a module or namespace body;
- ``class``: a member of a class body (or of a Ruby module body);
- ``local``: anything else, such as a statement inside a function or inside an ``if`` block.

Functions and classes are definitions at every level; a function at ``class`` level is a method
(every ``def`` inside a Ruby class or module). Variables and constants count only at ``top``
level, Ruby constants also at ``class`` level. Local variables and class fields are not symbols,
except a JavaScript or TypeScript class field whose value is a function, which is a method.
"""

import functools
import posixpath
from collections.abc import Callable
from typing import Final

import tree_sitter
import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_ruby
import tree_sitter_typescript
from tree_sitter import Node

from memini.chunking import more_languages
from memini.chunking.model import LANGUAGES, Language, SymbolKind
from memini.chunking.syntax import Found, Level, Spec
from memini.chunking.syntax import found as _found
from memini.chunking.syntax import name_of as _name
from memini.chunking.syntax import rewrap as _rewrap
from memini.chunking.syntax import text_of as _text

__all__ = [
    "EXTENSIONS",
    "GRAMMAR_PACKAGES",
    "Found",
    "Level",
    "Spec",
    "detect_language",
    "grammar",
    "spec",
]

EXTENSIONS: Final[dict[str, Language]] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".rb": "ruby",
    ".go": "go",
    **more_languages.EXTENSIONS,
}
"""File extensions, lowercase, and the language of each."""

GRAMMAR_PACKAGES: Final = (
    "tree-sitter",
    "tree-sitter-go",
    "tree-sitter-javascript",
    "tree-sitter-python",
    "tree-sitter-ruby",
    "tree-sitter-typescript",
    *more_languages.GRAMMAR_PACKAGES,
)
"""Distributions whose versions can change the chunker's output."""


def detect_language(path: str) -> Language | None:
    """The language of ``path`` by its extension (any case), or None if it has no grammar."""
    return EXTENSIONS.get(posixpath.splitext(path)[1].lower())


_GRAMMARS: Final[dict[str, Callable[[], object]]] = {
    "python": tree_sitter_python.language,
    "javascript": tree_sitter_javascript.language,
    "typescript": tree_sitter_typescript.language_typescript,
    "tsx": tree_sitter_typescript.language_tsx,
    "ruby": tree_sitter_ruby.language,
    "go": tree_sitter_go.language,
    **more_languages.GRAMMARS,
}


@functools.cache
def _grammar(name: str) -> tree_sitter.Language:
    return tree_sitter.Language(_GRAMMARS[name]())


def grammar(language: Language, path: str) -> tree_sitter.Language:
    """The grammar for ``language``; for TypeScript, the TSX grammar when ``path`` ends in .tsx."""
    if language == "typescript" and path.lower().endswith(".tsx"):
        return _grammar("tsx")
    return _grammar(language)


# Python ------------------------------------------------------------------------------------------


def _python_definition(core: Node, span: Node, level: Level) -> list[Found]:
    name = _name(core)
    body = core.child_by_field_name("body")
    if core.type == "class_definition":
        return _found(name, "class", span, body, container="class")
    return _found(name, "method" if level == "class" else "function", span, body)


def _python(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "decorated_definition":
        inner = node.child_by_field_name("definition")
        return [] if inner is None else _python_definition(inner, node, level)
    if kind in ("function_definition", "class_definition"):
        return _python_definition(node, node, level)
    if level != "top":
        return []
    if kind == "expression_statement" and node.named_child_count == 1:
        assignment = node.named_children[0]
        left = assignment.child_by_field_name("left")
        if assignment.type == "assignment" and left is not None and left.type == "identifier":
            name = _text(left)
            constant = name.upper() == name and any(c.isalpha() for c in name)
            return _found(name, "constant" if constant else "variable", node)
    if kind == "type_alias_statement":
        return _found(_text(node.child_by_field_name("left")), "type", node)
    return []


# JavaScript and TypeScript -----------------------------------------------------------------------

_JS_FUNCTIONS: Final = frozenset(
    {"arrow_function", "function", "function_expression", "generator_function"}
)


def _js_value(name: str, value: Node | None, span: Node, kind: SymbolKind) -> list[Found]:
    """``name`` bound to ``value``: a function or a class by its value, else of ``kind``.

    ``kind`` is ``method`` for a class field, where only a function value is a definition.
    """
    if value is not None and value.type in _JS_FUNCTIONS:
        body = value.child_by_field_name("body")
        return _found(name, "method" if kind == "method" else "function", span, body)
    if value is not None and value.type == "class":
        return _found(name, "class", span, value.child_by_field_name("body"), container="class")
    return [] if kind == "method" else _found(name, kind, span)


def _js_declarations(node: Node) -> list[Found]:
    """``const``, ``let`` and ``var`` at top level: one definition per declared name."""
    declarators = [c for c in node.named_children if c.type == "variable_declarator"]
    kind: SymbolKind = "constant" if node.children[0].type == "const" else "variable"
    found: list[Found] = []
    for declarator in declarators:
        name = declarator.child_by_field_name("name")
        if name is None or name.type != "identifier":
            continue  # a destructuring pattern
        span = node if len(declarators) == 1 else declarator
        found += _js_value(_text(name), declarator.child_by_field_name("value"), span, kind)
    return found


def _js_wrapped(node: Node, level: Level) -> list[Found]:
    """An ``export``, ``declare`` or namespace statement: the definitions inside, spanning it."""
    if node.type == "export_statement":
        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            return _rewrap(_js(declaration, level), declaration, node)
        value = node.child_by_field_name("value")  # export default <value>
        if value is None or (value.type not in _JS_FUNCTIONS and value.type != "class"):
            return []
        name = _text(value.child_by_field_name("name")) or "default"
        return _js_value(name, value, node, "variable")
    inner = next((c for c in node.named_children if c.type != "comment"), None)
    if inner is None or (node.type == "expression_statement" and inner.type != "internal_module"):
        return []
    return _rewrap(_js(inner, level), inner, node)


def _js_member(node: Node) -> list[Found]:
    """A member of a class body."""
    name = _name(node)
    if node.type == "method_definition":
        return _found(name, "method", node, node.child_by_field_name("body"))
    if node.type in ("method_signature", "abstract_method_signature"):
        return _found(name, "method", node)
    if node.type in ("public_field_definition", "field_definition"):
        name = name or _text(node.child_by_field_name("property"))
        return _js_value(name, node.child_by_field_name("value"), node, "method")
    return []


_JS_TYPES: Final[dict[str, SymbolKind]] = {
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}


def _js(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in ("export_statement", "ambient_declaration", "expression_statement"):
        return _js_wrapped(node, level)
    if kind in ("function_declaration", "generator_function_declaration", "function_signature"):
        return _found(_name(node), "function", node, node.child_by_field_name("body"))
    if kind in ("class_declaration", "abstract_class_declaration", "class"):
        body = node.child_by_field_name("body")
        return _found(_name(node), "class", node, body, container="class")
    if kind in ("internal_module", "module"):  # namespace N.M { }, declare module "m" { }
        name = node.child_by_field_name("name")
        if name is not None and name.type == "string":
            path = (_text(name)[1:-1],)
        else:
            path = tuple(part for part in _text(name).split(".") if part)
        if not path or not all(path):
            return []
        body = node.child_by_field_name("body")
        return [Found(path, "module", node, body, body_level="top", container=True)]
    if kind in _JS_TYPES:
        return _found(_name(node), _JS_TYPES[kind], node)
    if level == "class":
        return _js_member(node)
    if level == "top" and kind in ("lexical_declaration", "variable_declaration"):
        return _js_declarations(node)
    return []


# Ruby --------------------------------------------------------------------------------------------


def _ruby(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in ("class", "module"):
        path = tuple(part for part in _text(node.child_by_field_name("name")).split("::") if part)
        if not path:
            return []
        symbol_kind: SymbolKind = "class" if kind == "class" else "module"
        body = node.child_by_field_name("body")
        return [Found(path, symbol_kind, node, body, body_level="class", container=True)]
    if kind in ("method", "singleton_method"):
        method = level == "class" or kind == "singleton_method"
        body = node.child_by_field_name("body")
        return _found(_name(node), "method" if method else "function", node, body)
    if kind == "assignment" and level != "local":
        left = node.child_by_field_name("left")
        if left is not None and left.type == "constant":
            return _found(_text(left), "constant", node)
    return []


def _ruby_anonymous_container(node: Node) -> Node | None:
    return node.child_by_field_name("body") if node.type == "singleton_class" else None


# Go ----------------------------------------------------------------------------------------------


def _go_receiver(node: Node) -> str:
    """The type name of a method receiver: ``Point`` for ``(p *Point)`` or ``(s Stack[T])``."""
    receiver = node.child_by_field_name("receiver")
    if receiver is None:
        return ""
    parameter = next(
        (c for c in receiver.named_children if c.type == "parameter_declaration"), None
    )
    found = None if parameter is None else parameter.child_by_field_name("type")
    while found is not None and found.type in (
        "pointer_type",
        "generic_type",
        "parenthesized_type",
    ):
        if found.type == "generic_type":
            found = found.child_by_field_name("type")
        else:
            found = next(iter(found.named_children), None)
    return _text(found)


def _go_specs(node: Node, types: tuple[str, ...]) -> list[Node]:
    """The specs of a declaration, whether grouped in parentheses or not."""
    specs: list[Node] = []
    for child in node.named_children:
        if child.type in types:
            specs.append(child)
        elif child.type.endswith("_spec_list"):
            specs += [c for c in child.named_children if c.type in types]
    return specs


def _go_type_kind(spec_node: Node) -> SymbolKind:
    shape = spec_node.child_by_field_name("type")
    if spec_node.type == "type_spec" and shape is not None:
        if shape.type == "struct_type":
            return "struct"
        if shape.type == "interface_type":
            return "interface"
    return "type"


def _go(node: Node, level: Level) -> list[Found]:
    kind = node.type
    # Go has no nested named functions, so function bodies are not searched.
    if kind == "function_declaration":
        return _found(_name(node), "function", node)
    if kind == "method_declaration":
        name = _name(node)
        receiver = _go_receiver(node)
        if not name:
            return []
        return [Found((receiver, name) if receiver else (name,), "method", node)]
    if level != "top":
        return []
    found: list[Found] = []
    if kind == "type_declaration":
        specs = _go_specs(node, ("type_spec", "type_alias"))
        for spec_node in specs:
            span = node if len(specs) == 1 else spec_node
            type_name = _text(spec_node.child_by_field_name("name"))
            found += _found(type_name, _go_type_kind(spec_node), span)
    elif kind in ("const_declaration", "var_declaration"):
        specs = _go_specs(node, ("const_spec", "var_spec"))
        value_kind: SymbolKind = "constant" if kind == "const_declaration" else "variable"
        for spec_node in specs:
            span = node if len(specs) == 1 else spec_node
            for name_node in spec_node.children_by_field_name("name"):
                found += _found(_text(name_node), value_kind, span)
    return found


# -------------------------------------------------------------------------------------------------

_SPECS: Final[dict[Language, Spec]] = {
    "python": Spec("python", _python),
    "javascript": Spec("javascript", _js, decorators=frozenset({"decorator"})),
    "typescript": Spec("typescript", _js, decorators=frozenset({"decorator"})),
    "ruby": Spec("ruby", _ruby, anonymous_container=_ruby_anonymous_container),
    "go": Spec("go", _go),
    **more_languages.SPECS,
}
assert sorted(_SPECS) == sorted(LANGUAGES)  # noqa: S101 -- one spec per language


def spec(language: Language) -> Spec:
    """How the chunker reads ``language``."""
    return _SPECS[language]
