"""Fifteen more languages: Java, C#, C, C++, PHP, Kotlin, Swift, Rust, Scala, Dart, Lua, shell, SQL,
Objective-C and Elixir. :mod:`memini.chunking.languages` describes the first five and merges these.

Each language is a ``classify`` function over the node types its grammar uses for definitions;
the types follow the ``tags.scm`` queries the grammars ship. The queries themselves are not used:
four of the fifteen packages ship none, and a query run on every node of the walk costs a pass over
the subtree each time. The rules are those of the first five languages:

- types, functions and modules are definitions at every level, and a function at ``class`` level
  is a method;
- variables and constants count at ``top`` level only; class fields and properties are not
  symbols;
- a body that adds members to a type defined elsewhere (a Rust ``impl``, a Swift or Dart
  ``extension``, an Objective-C ``@implementation``) defines no symbol of its own: its members
  are named after that type (``Mill.grind``), as a Go method is named after its receiver;
- a C++ definition written outside its class (``bool Dock::moor()``) is named the same way;
- the wrappers of C and C++ (header guards and other ``#if`` blocks, ``extern "C" { }``) are
  transparent: what they hold sits at their own level.
"""

import re
from collections.abc import Callable
from typing import Final

import tree_sitter_bash
import tree_sitter_c
import tree_sitter_c_sharp
import tree_sitter_cpp
import tree_sitter_dart
import tree_sitter_elixir
import tree_sitter_java
import tree_sitter_kotlin
import tree_sitter_lua
import tree_sitter_objc
import tree_sitter_php
import tree_sitter_rust
import tree_sitter_scala
import tree_sitter_sql
import tree_sitter_swift
from tree_sitter import Node

from memini.chunking.model import Language, SymbolKind
from memini.chunking.syntax import Found, Level, Spec, found, name_of, rewrap, text_of

__all__ = ["EXTENSIONS", "GRAMMARS", "GRAMMAR_PACKAGES", "SPECS"]

EXTENSIONS: Final[dict[str, Language]] = {
    ".java": "java",
    ".cs": "csharp",
    ".c": "c",
    ".h": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".h++": "cpp",
    ".php": "php",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".rs": "rust",
    ".scala": "scala",
    ".sc": "scala",
    ".dart": "dart",
    ".lua": "lua",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".m": "objc",
    ".mm": "objc",
    ".ex": "elixir",
    ".exs": "elixir",
}
"""File extensions of these languages. A ``.h`` header is read as C++, which also parses C."""

GRAMMAR_PACKAGES: Final = (
    "tree-sitter-bash",
    "tree-sitter-c",
    "tree-sitter-c-sharp",
    "tree-sitter-cpp",
    "tree-sitter-dart",
    "tree-sitter-elixir",
    "tree-sitter-java",
    "tree-sitter-kotlin",
    "tree-sitter-lua",
    "tree-sitter-objc",
    "tree-sitter-php",
    "tree-sitter-rust",
    "tree-sitter-scala",
    "tree-sitter-sql",
    "tree-sitter-swift",
)

GRAMMARS: Final[dict[str, Callable[[], object]]] = {
    "java": tree_sitter_java.language,
    "csharp": tree_sitter_c_sharp.language,
    "c": tree_sitter_c.language,
    "cpp": tree_sitter_cpp.language,
    "php": tree_sitter_php.language_php,
    "kotlin": tree_sitter_kotlin.language,
    "swift": tree_sitter_swift.language,
    "rust": tree_sitter_rust.language,
    "scala": tree_sitter_scala.language,
    "dart": tree_sitter_dart.language,
    "lua": tree_sitter_lua.language,
    "shell": tree_sitter_bash.language,
    "sql": tree_sitter_sql.language,
    "objc": tree_sitter_objc.language,
    "elixir": tree_sitter_elixir.language,
}


# Helpers -----------------------------------------------------------------------------------------


def _member(level: Level) -> SymbolKind:
    """A function at ``class`` level is a method."""
    return "method" if level == "class" else "function"


def _child(node: Node, *types: str) -> Node | None:
    """The first named child of ``node`` of one of ``types``."""
    return next((c for c in node.named_children if c.type in types), None)


def _parts(text: str, separator: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(separator) if part.strip())


def _path(
    path: tuple[str, ...],
    kind: SymbolKind,
    span: Node,
    body: Node | None = None,
    *,
    container: Level | None = None,
    end: Node | None = None,
) -> list[Found]:
    """One definition named by ``path``, or none if a part is missing (a syntax error)."""
    if not path or not all(path):
        return []
    if container is None:
        return [Found(path, kind, span, body, end=end)]
    return [Found(path, kind, span, body, body_level=container, container=True, end=end)]


def _ancestor(node: Node, types: frozenset[str], depth: int = 3) -> Node | None:
    """The nearest of the first ``depth`` ancestors of ``node`` whose type is one of ``types``."""
    parent = node.parent
    for _ in range(depth):
        if parent is None:
            return None
        if parent.type in types:
            return parent
        parent = parent.parent
    return None


def _named(qualifier: str, name: str) -> tuple[str, ...]:
    return (qualifier, name) if qualifier else (name,)


def _constant_or_variable(name: str) -> SymbolKind:
    """A name in capitals is a constant, as in Python."""
    return "constant" if name.upper() == name and any(c.isalpha() for c in name) else "variable"


# Java --------------------------------------------------------------------------------------------

_JAVA_TYPES: Final[dict[str, SymbolKind]] = {
    "class_declaration": "class",
    "record_declaration": "class",
    "interface_declaration": "interface",
    "annotation_type_declaration": "interface",
    "enum_declaration": "enum",
}


def _java(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in _JAVA_TYPES:
        body = node.child_by_field_name("body")
        return found(name_of(node), _JAVA_TYPES[kind], node, body, container="class")
    if kind in ("method_declaration", "constructor_declaration", "compact_constructor_declaration"):
        return found(name_of(node), _member(level), node, node.child_by_field_name("body"))
    return []


def _java_anonymous(node: Node) -> Node | None:
    # The methods of an enum stand after its constants, in a node of their own.
    return node if node.type == "enum_body_declarations" else None


# C# ----------------------------------------------------------------------------------------------

_CSHARP_TYPES: Final[dict[str, SymbolKind]] = {
    "class_declaration": "class",
    "record_declaration": "class",
    "struct_declaration": "struct",
    "record_struct_declaration": "struct",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}


def _csharp(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "namespace_declaration":
        path = _parts(text_of(node.child_by_field_name("name")), ".")
        return _path(path, "module", node, node.child_by_field_name("body"), container="top")
    if kind in _CSHARP_TYPES:
        body = node.child_by_field_name("body")
        return found(name_of(node), _CSHARP_TYPES[kind], node, body, container="class")
    if kind in ("method_declaration", "constructor_declaration", "destructor_declaration"):
        return found(name_of(node), _member(level), node, node.child_by_field_name("body"))
    if kind == "local_function_statement":
        return found(name_of(node), "function", node, node.child_by_field_name("body"))
    if kind == "delegate_declaration":
        return found(name_of(node), "type", node)
    return []


# C, C++ and Objective-C ---------------------------------------------------------------------------

_DECLARATOR_WRAPPERS: Final = frozenset(
    {
        "function_declarator",
        "pointer_declarator",
        "reference_declarator",
        "array_declarator",
        "parenthesized_declarator",
        "init_declarator",
        "attributed_declarator",
    }
)
_DECLARATOR_NAMES: Final = frozenset(
    {
        "identifier",
        "field_identifier",
        "type_identifier",
        "destructor_name",
        "operator_name",
        "qualified_identifier",
        "namespace_identifier",
    }
)
_TEMPLATE_ARGUMENTS: Final = re.compile(r"<[^<>]*>")


def _declarator(node: Node | None) -> tuple[tuple[str, ...], bool]:
    """The name a C declarator declares, as a path (``Dock::moor`` is ``("Dock", "moor")``), and
    whether it declares a function."""
    function = False
    while node is not None and node.type in _DECLARATOR_WRAPPERS:
        function = function or node.type == "function_declarator"
        inner = node.child_by_field_name("declarator")
        node = inner if inner is not None else next(iter(node.named_children), None)
    if node is None or node.type not in _DECLARATOR_NAMES:
        return (), function
    text = text_of(node)
    while _TEMPLATE_ARGUMENTS.search(text):
        text = _TEMPLATE_ARGUMENTS.sub("", text)
    return _parts(text, "::"), function


def _c_variables(node: Node) -> list[Found]:
    """A declaration at file level: a function prototype, or variables and constants."""
    declarators = node.children_by_field_name("declarator")
    constant = any(
        c.type == "type_qualifier" and text_of(c) in ("const", "constexpr") for c in node.children
    )
    definitions: list[Found] = []
    for declarator in declarators:
        path, function = _declarator(declarator)
        span = node if len(declarators) == 1 else declarator
        if function:
            kind: SymbolKind = "method" if len(path) > 1 else "function"
        else:
            kind = "constant" if constant else "variable"
        definitions += _path(path, kind, span)
    return definitions


_C_TYPES: Final[dict[str, SymbolKind]] = {
    "struct_specifier": "struct",
    "union_specifier": "struct",
    "enum_specifier": "enum",
    "class_specifier": "class",
}


def _c(node: Node, level: Level) -> list[Found]:
    """C, and the C part of C++ and Objective-C."""
    kind = node.type
    if kind == "function_definition":
        path, _ = _declarator(node.child_by_field_name("declarator"))
        method = level == "class" or len(path) > 1
        return _path(path, "method" if method else "function", node)
    if kind in _C_TYPES:
        body = node.child_by_field_name("body")
        if body is None:
            return []  # struct S used as a type, not defined
        name = node.child_by_field_name("name")
        path = _parts(text_of(name), "::")
        return _path(path, _C_TYPES[kind], node, body, container="class")
    if kind in ("type_definition", "alias_declaration"):
        if kind == "alias_declaration":
            return found(name_of(node), "type", node)
        path, _ = _declarator(node.child_by_field_name("declarator"))
        return _path(path, "type", node)
    if kind == "preproc_def":
        return found(name_of(node), "constant", node)
    if kind == "preproc_function_def":
        return found(name_of(node), "function", node)
    if kind == "declaration" and level == "top":
        return _c_variables(node)
    return []


_C_TRANSPARENT: Final = frozenset(
    {"preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "preproc_elifdef"}
)


def _c_transparent(node: Node) -> Node | None:
    if node.type in _C_TRANSPARENT:
        return node
    if node.type == "linkage_specification":  # extern "C" { ... }
        return node.child_by_field_name("body")
    if node.type == "namespace_definition" and node.child_by_field_name("name") is None:
        return node.child_by_field_name("body")  # an anonymous namespace
    return None


def _cpp(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "namespace_definition":
        name = node.child_by_field_name("name")
        if name is None:
            return []
        path = _parts(text_of(name), "::")
        return _path(path, "module", node, node.child_by_field_name("body"), container="top")
    if kind == "template_declaration":
        inner = next(
            (
                c
                for c in node.named_children
                if c.type not in ("template_parameter_list", "comment")
            ),
            None,
        )
        return [] if inner is None else rewrap(_cpp(inner, level), inner, node)
    if kind == "concept_definition":
        return found(name_of(node), "type", node)
    if kind in ("field_declaration", "declaration") and level == "class":
        path, function = _declarator(node.child_by_field_name("declarator"))
        return _path(path, "method", node) if function else []
    return _c(node, level)


_OBJC_IMPLEMENTATIONS: Final = frozenset(
    {"class_implementation", "category_implementation", "category_interface"}
)


def _objc_selector(node: Node) -> str:
    return text_of(_child(node, "identifier"))


def _objc(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in ("class_interface", "protocol_declaration"):
        symbol: SymbolKind = "class" if kind == "class_interface" else "interface"
        return found(_objc_selector(node), symbol, node, node, container="class")
    if kind == "implementation_definition":
        inner = next(iter(node.named_children), None)
        return [] if inner is None else rewrap(_objc(inner, level), inner, node)
    if kind in ("method_declaration", "method_definition"):
        container = _ancestor(node, _OBJC_IMPLEMENTATIONS)
        qualifier = "" if container is None else _objc_selector(container)
        return _path(_named(qualifier, _objc_selector(node)), "method", node)
    return _c(node, level)


def _objc_anonymous(node: Node) -> Node | None:
    return node if node.type in _OBJC_IMPLEMENTATIONS else None


# PHP ---------------------------------------------------------------------------------------------

_PHP_TYPES: Final[dict[str, SymbolKind]] = {
    "class_declaration": "class",
    "trait_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}


def _php(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "namespace_definition":
        body = node.child_by_field_name("body")
        if body is None:
            return []  # namespace N; names the file, like a Java package
        path = _parts(text_of(node.child_by_field_name("name")), "\\")
        return _path(path, "module", node, body, container="top")
    if kind in _PHP_TYPES:
        body = node.child_by_field_name("body")
        return found(name_of(node), _PHP_TYPES[kind], node, body, container="class")
    if kind == "method_declaration":
        return found(name_of(node), "method", node, node.child_by_field_name("body"))
    if kind == "function_definition":
        return found(name_of(node), "function", node, node.child_by_field_name("body"))
    if kind == "const_declaration" and level == "top":
        elements = [c for c in node.named_children if c.type == "const_element"]
        definitions: list[Found] = []
        for element in elements:
            span = node if len(elements) == 1 else element
            definitions += found(text_of(_child(element, "name")), "constant", span)
        return definitions
    return []


# Kotlin ------------------------------------------------------------------------------------------


def _kotlin_class_kind(node: Node) -> SymbolKind:
    if any(c.type == "interface" for c in node.children):
        return "interface"
    modifiers = _child(node, "modifiers")
    if modifiers is not None and "enum" in text_of(modifiers).split():
        return "enum"
    return "class"


def _kotlin(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in ("class_declaration", "object_declaration"):
        body = _child(node, "class_body", "enum_class_body")
        symbol = _kotlin_class_kind(node) if kind == "class_declaration" else "class"
        return found(name_of(node), symbol, node, body, container="class")
    if kind == "function_declaration":
        name = name_of(node)
        body = _child(node, "function_body")
        receiver = _child(node, "user_type")
        dot = next((c for c in node.children if c.type == "."), None)
        if receiver is not None and dot is not None and receiver.end_byte <= dot.start_byte:
            return _path((text_of(receiver), name), "method", node, body)  # fun Kite.launch()
        return found(name, _member(level), node, body)
    if kind == "type_alias":
        return found(text_of(_child(node, "identifier", "type_identifier")), "type", node)
    if kind == "property_declaration" and level == "top":
        variable = _child(node, "variable_declaration")
        name = text_of(None if variable is None else _child(variable, "identifier"))
        modifiers = _child(node, "modifiers")
        const = modifiers is not None and "const" in text_of(modifiers).split()
        return found(name, "constant" if const else _constant_or_variable(name), node)
    return []


def _kotlin_anonymous(node: Node) -> Node | None:
    return _child(node, "class_body") if node.type == "companion_object" else None


# Swift -------------------------------------------------------------------------------------------

_SWIFT_KINDS: Final[dict[str, SymbolKind]] = {
    "class": "class",
    "actor": "class",
    "struct": "struct",
    "enum": "enum",
}


def _swift_kind(node: Node) -> str:
    return text_of(node.child_by_field_name("declaration_kind"))


def _swift(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "class_declaration":
        symbol = _SWIFT_KINDS.get(_swift_kind(node))
        if symbol is None:
            return []  # an extension: its members are named after the type
        body = node.child_by_field_name("body")
        return found(name_of(node), symbol, node, body, container="class")
    if kind == "protocol_declaration":
        body = node.child_by_field_name("body")
        return found(name_of(node), "interface", node, body, container="class")
    if kind in ("function_declaration", "protocol_function_declaration"):
        name = text_of(node.child_by_field_name("name"))
        extension = _ancestor(node, frozenset({"class_declaration"}), depth=2)
        if extension is not None and _swift_kind(extension) == "extension":
            path = (text_of(extension.child_by_field_name("name")), name)
            return _path(path, "method", node, node.child_by_field_name("body"))
        return found(name, _member(level), node, node.child_by_field_name("body"))
    if kind in ("init_declaration", "deinit_declaration"):
        word = "init" if kind == "init_declaration" else "deinit"
        return found(word, "method", node, node.child_by_field_name("body"))
    if kind == "typealias_declaration":
        return found(name_of(node), "type", node)
    if kind == "property_declaration" and level == "top":
        binding = text_of(_child(node, "value_binding_pattern"))
        name = text_of(node.child_by_field_name("name"))
        return found(name, "constant" if binding == "let" else "variable", node)
    return []


def _swift_anonymous(node: Node) -> Node | None:
    if node.type == "class_declaration" and _swift_kind(node) == "extension":
        return node.child_by_field_name("body")
    return None


# Rust --------------------------------------------------------------------------------------------

_RUST_TYPES: Final[dict[str, SymbolKind]] = {
    "struct_item": "struct",
    "union_item": "struct",
    "enum_item": "enum",
    "trait_item": "interface",
}


def _rust_self_type(impl: Node) -> str:
    """The type an ``impl`` block is for: ``Mill`` in ``impl<T> Grind for Mill<T>``."""
    found_type = impl.child_by_field_name("type")
    while found_type is not None and found_type.type in ("generic_type", "scoped_type_identifier"):
        inner = found_type.child_by_field_name(
            "type" if found_type.type == "generic_type" else "name"
        )
        found_type = inner
    return text_of(found_type)


def _rust(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in ("function_item", "function_signature_item"):
        name = name_of(node)
        body = node.child_by_field_name("body")
        impl = _ancestor(node, frozenset({"impl_item"}), depth=2)
        if impl is not None:
            return _path(_named(_rust_self_type(impl), name), "method", node, body)
        return found(name, _member(level), node, body)
    if kind in _RUST_TYPES:
        body = node.child_by_field_name("body")
        return found(name_of(node), _RUST_TYPES[kind], node, body, container="class")
    if kind == "mod_item":
        body = node.child_by_field_name("body")
        return found(name_of(node), "module", node, body, container="top")
    if kind == "type_item":
        return found(name_of(node), "type", node)
    if kind == "macro_definition":
        return found(name_of(node), "function", node)
    if kind in ("const_item", "static_item") and level == "top":
        return found(name_of(node), "constant" if kind == "const_item" else "variable", node)
    return []


def _rust_anonymous(node: Node) -> Node | None:
    return node.child_by_field_name("body") if node.type == "impl_item" else None


# Scala -------------------------------------------------------------------------------------------

_SCALA_TYPES: Final[dict[str, SymbolKind]] = {
    "class_definition": "class",
    "object_definition": "class",
    "trait_definition": "interface",
    "enum_definition": "enum",
}


def _scala(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in _SCALA_TYPES:
        body = node.child_by_field_name("body")
        return found(name_of(node), _SCALA_TYPES[kind], node, body, container="class")
    if kind in ("function_definition", "function_declaration"):
        return found(name_of(node), _member(level), node, node.child_by_field_name("body"))
    if kind == "type_definition":
        return found(name_of(node), "type", node)
    if kind in ("val_definition", "var_definition") and level == "top":
        pattern = node.child_by_field_name("pattern")
        if pattern is None or pattern.type != "identifier":
            return []
        return found(text_of(pattern), "constant" if kind == "val_definition" else "variable", node)
    return []


# Dart --------------------------------------------------------------------------------------------

_DART_SIGNATURES: Final = frozenset(
    {
        "function_signature",
        "getter_signature",
        "setter_signature",
        "constructor_signature",
        "factory_constructor_signature",
        "operator_signature",
        "constant_constructor_signature",
        "redirecting_factory_constructor_signature",
    }
)


def _dart_body(node: Node) -> Node | None:
    """The body that follows a signature: the grammar puts it beside it, not in it."""
    after = node.next_named_sibling
    return after if after is not None and after.type == "function_body" else None


def _dart_method(node: Node, signature: Node) -> list[Found]:
    name = text_of(signature.child_by_field_name("name") or _child(signature, "identifier"))
    extension = _ancestor(node, frozenset({"extension_declaration"}), depth=2)
    qualifier = "" if extension is None else text_of(extension.child_by_field_name("class"))
    return _path(_named(qualifier, name), "method", node, end=_dart_body(node))


def _dart(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind in ("class_definition", "mixin_declaration"):
        name = name_of(node) or text_of(_child(node, "identifier"))
        body = node.child_by_field_name("body") or _child(node, "class_body")
        return found(name, "class", node, body, container="class")
    if kind == "enum_declaration":
        body = node.child_by_field_name("body")
        return found(name_of(node), "enum", node, body, container="class")
    if kind == "function_signature" and level != "class":
        return found(name_of(node), "function", node, end=_dart_body(node))
    if kind in ("method_signature", "declaration") and level == "class":
        signature = _child(node, *_DART_SIGNATURES)
        return [] if signature is None else _dart_method(node, signature)
    if kind == "type_alias":
        return found(text_of(_child(node, "type_identifier")), "type", node)
    if kind == "static_final_declaration_list" and level == "top":
        declarations = [c for c in node.named_children if c.type == "static_final_declaration"]
        definitions: list[Found] = []
        for declaration in declarations:
            span = node if len(declarations) == 1 else declaration
            definitions += found(text_of(_child(declaration, "identifier")), "constant", span)
        return definitions
    return []


def _dart_anonymous(node: Node) -> Node | None:
    return node.child_by_field_name("body") if node.type == "extension_declaration" else None


# Lua ---------------------------------------------------------------------------------------------


def _lua(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "function_declaration":
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is not None and name.type == "method_index_expression":  # function M:count()
            return _path(_parts(text_of(name), ":"), "method", node, body)
        return _path(_parts(text_of(name), "."), "function", node, body)
    if kind == "variable_declaration" and level == "top":
        assignment = _child(node, "assignment_statement")
        names = _child(assignment or node, "variable_list", "attribute_name_list")
        identifiers = [] if names is None else [c for c in names.named_children]
        definitions: list[Found] = []
        for identifier in identifiers:
            if identifier.type != "identifier":
                continue
            name = text_of(identifier)
            span = node if len(identifiers) == 1 else identifier
            definitions += found(name, _constant_or_variable(name), span)
        return definitions
    return []


# Shell -------------------------------------------------------------------------------------------


def _shell(node: Node, level: Level) -> list[Found]:
    kind = node.type
    if kind == "function_definition":
        return found(name_of(node), "function", node, node.child_by_field_name("body"))
    if kind == "variable_assignment" and level == "top":
        name = name_of(node)
        return found(name, _constant_or_variable(name), node)
    return []


# SQL ---------------------------------------------------------------------------------------------

_SQL_CREATES: Final[dict[str, SymbolKind]] = {
    "create_table": "type",
    "create_view": "type",
    "create_materialized_view": "type",
    "create_type": "type",
    "create_function": "function",
    "create_procedure": "function",
    "create_trigger": "function",
}


def _sql(node: Node, level: Level) -> list[Found]:
    if node.type != "statement" or level != "top":
        return []
    create = _child(node, *_SQL_CREATES)
    if create is None:
        return []
    name = _child(create, "object_reference")
    if name is None and create.type == "create_trigger":
        name = _child(create, "identifier")
    return _path(_parts(text_of(name), "."), _SQL_CREATES[create.type], node)


# Elixir ------------------------------------------------------------------------------------------

_ELIXIR_MODULES: Final = frozenset({"defmodule", "defprotocol", "defimpl"})
_ELIXIR_FUNCTIONS: Final = frozenset(
    {"def", "defp", "defmacro", "defmacrop", "defguard", "defguardp", "defdelegate", "defn"}
)


def _elixir_function_name(arguments: Node) -> str:
    """The name in ``def name(args) when guard``, ``def name, do: ...`` and the like."""
    head = next(iter(arguments.named_children), None)
    if head is not None and head.type == "binary_operator":  # a guard: name(args) when ...
        head = head.child_by_field_name("left")
    if head is not None and head.type == "call":
        head = head.child_by_field_name("target")
    return text_of(head) if head is not None and head.type == "identifier" else ""


def _elixir(node: Node, level: Level) -> list[Found]:
    if node.type != "call":
        return []
    target = text_of(node.child_by_field_name("target"))
    arguments = _child(node, "arguments")
    if arguments is None:
        return []
    if target in _ELIXIR_MODULES:
        alias = _child(arguments, "alias")
        body = _child(node, "do_block")
        return _path(_parts(text_of(alias), "."), "module", node, body, container="class")
    if target in _ELIXIR_FUNCTIONS:
        return found(_elixir_function_name(arguments), "function", node)
    return []


# -------------------------------------------------------------------------------------------------

_C_COMMENTS: Final = frozenset({"comment"})

SPECS: Final[dict[Language, Spec]] = {
    "java": Spec(
        "java",
        _java,
        comments=frozenset({"line_comment", "block_comment"}),
        anonymous_container=_java_anonymous,
    ),
    "csharp": Spec("csharp", _csharp),
    "c": Spec("c", _c, comments=_C_COMMENTS, transparent=_c_transparent),
    "cpp": Spec("cpp", _cpp, comments=_C_COMMENTS, transparent=_c_transparent),
    "php": Spec("php", _php),
    "kotlin": Spec(
        "kotlin",
        _kotlin,
        comments=frozenset({"line_comment", "multiline_comment", "block_comment"}),
        anonymous_container=_kotlin_anonymous,
    ),
    "swift": Spec(
        "swift",
        _swift,
        comments=frozenset({"comment", "multiline_comment"}),
        anonymous_container=_swift_anonymous,
    ),
    "rust": Spec(
        "rust",
        _rust,
        comments=frozenset({"line_comment", "block_comment"}),
        decorators=frozenset({"attribute_item"}),
        anonymous_container=_rust_anonymous,
    ),
    "scala": Spec("scala", _scala, comments=frozenset({"comment", "block_comment"})),
    "dart": Spec(
        "dart",
        _dart,
        comments=frozenset({"comment", "documentation_comment"}),
        decorators=frozenset({"annotation"}),
        anonymous_container=_dart_anonymous,
    ),
    "lua": Spec("lua", _lua),
    "shell": Spec("shell", _shell),
    "sql": Spec("sql", _sql, comments=frozenset({"comment", "marginalia"})),
    "objc": Spec(
        "objc",
        _objc,
        comments=_C_COMMENTS,
        anonymous_container=_objc_anonymous,
        transparent=_c_transparent,
    ),
    "elixir": Spec("elixir", _elixir),
}
"""How the chunker reads each of these languages."""
