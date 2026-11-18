"""The recipes that ship with Memini: one ``<name>.toml`` file per recipe in this directory.

An mcp source names one with ``{"recipe": {"bundled": "<name>"}}`` (``memini source add --kind
mcp --recipe <name>``), and every sync reads it from here, so a recipe fixed in a new build applies
to the sources that use it; a source can also hold a recipe of its own
(:mod:`memini.executors.source`).
Each recipe's parser is registered in the same build (:func:`memini.recipes.register_parser`). The
recipes of a chat server, a mail server and a note-taker come with their parsers (milestone M3).
"""
