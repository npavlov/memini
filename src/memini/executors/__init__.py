"""The executors of MCP recipes (requirements, section 4, MCP recipes; docs/recipes.md, How the
executors use a recipe).

One recipe, two executors. The service executor (:mod:`memini.executors.service`) runs a recipe on
a schedule with the built-in MCP client; the agent executor (:mod:`memini.executors.agent`) hands
the same calls out to an agent session and stores what its capture hook posts. Both read a
source's recipe through :mod:`memini.executors.source`, find their calls with
:func:`memini.recipes.missing_calls`, store raw responses and end a window with
:mod:`memini.responses`, so one recipe run by both yields the same records.

- :mod:`memini.executors.source`: what an mcp source is (its endpoint, its recipe, its window
  overrides) as ``source.config`` holds it, and the checks a source passes when it is added.
- :mod:`memini.executors.core`: the steps around a call: the recipe of a source and its parser,
  its window settings, the drift check, the calls to make next.
- :mod:`memini.executors.agent`: the agent executor's runs, next calls and captured responses.
- :mod:`memini.executors.egress`: where the service executor may connect: the endpoint allowlist,
  internal addresses, the stdio commands.
- :mod:`memini.executors.transport`: the HTTP transport that enforces it on every request.
- :mod:`memini.executors.budget`: the rate budget of a recipe and the backoff of retries.
- :mod:`memini.executors.client`: an MCP session with a source, over streamable HTTP or stdio.
- :mod:`memini.executors.oauth`: refreshing a source's OAuth tokens at its token endpoint.
- :mod:`memini.executors.service`: the ``mcp_sync`` job handler.

This package module imports nothing, so :mod:`memini.sources` can import
:mod:`memini.executors.source` without loading the client and the job code: import the
submodules.
"""
