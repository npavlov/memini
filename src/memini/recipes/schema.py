"""The JSON Schema of a recipe file as text, as committed in ``schemas/recipe.schema.json``.

Regenerate the committed file after a change of :mod:`memini.recipes.model`::

    uv run python -m memini.recipes.schema > schemas/recipe.schema.json

A test fails while the committed file and the model differ.
"""

import json

from memini.recipes.model import recipe_json_schema

__all__ = ["schema_text"]


def schema_text() -> str:
    """The schema as JSON, indented by two spaces, with a final newline."""
    return json.dumps(recipe_json_schema(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(schema_text(), end="")
