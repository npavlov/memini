"""The agent executor (requirements, section 4, MCP recipes): a recipe run for a server that is
reachable only inside an interactive agent session. The service plans and checks; the agent makes
the calls; a capture hook posts each raw response back verbatim; the same parser and checks run on
the server side. The agent never rewrites a payload, and no model reads one in the sync loop
(principle 5).

**A run** is one intake of one stream of an mcp source (:mod:`.session`, routes in
:mod:`memini.api.agent_runs`, tables in migration 0016):

1. :func:`start_run` pins the database time (``as_of``), plans the stream's windows
   (:func:`memini.intake.plan_stream`, the recipe's ``[window]`` with the source's overrides),
   and opens the first window (:func:`memini.responses.begin_window`). A stream has at most one
   unfinished run.
2. :func:`next_call` hands out one call at a time: the first of the current window's missing calls
   (:func:`memini.recipes.missing_calls` over what is stored), with an id. A call is made once
   per window unless it failed or got no response, and then at most :data:`MAX_ATTEMPTS` times; a
   call past ``max_pages`` is never handed out. A window with nothing left to hand out is done
   (``window_done``); one that is not complete stops its chain (the planner's chains: windows
   that move the checkpoint, windows that move the frontier). The next call opens the following
   window; with none left, or ``rate.max_calls`` calls handed out, the run is done (``run_done``).
3. :func:`submit_response` stores the raw result of a call it handed out
   (:func:`memini.responses.store_response`: redacted, otherwise verbatim). The response names the
   call's id, and its tool and arguments must be the call's: a response for a call not handed out,
   or for another call under its id, is refused and nothing is stored. The answer is a receipt:
   positions and counts, never a value of the response.
4. :func:`finish_run` closes the run to calls, parses and ingests each opened window in the order
   of the plan (:func:`memini.responses.ingest_window`), each chain stopping at its first
   incomplete window, and writes the run report (kind ``agent_sync``): the plan, the session's
   counts, a step per window, the incomplete windows with the calls that would complete them. It
   holds the source's job lock, as replay and forget do. A run can be finished at any point (the
   session ends): what was stored is parsed, and the incomplete windows keep the checkpoint.

**The recipe** of an mcp source is in its configuration (:data:`RECIPE_KEY`: the TOML text, or the
recipe as a JSON object), its window overrides under :data:`WINDOW_KEY`, and its parser is the one
registered under ``parser.name``. The run records the recipe digest; a recipe changed while a run
is open stops its calls (:class:`RecipeChangedError`), and finish parses with the recipe as it is.

**The server.** The agent cannot always tell what ``serverInfo`` its session reported; the run
records the recipe's server name, or the name and version the start names (another name is
refused, a version the recipe was not checked against is counted, ``server_version_unchecked``).

**The capture side** is not here: a Claude Code skill and hook in
``integrations/claude-code`` drive the loop through the HTTP API (docs/recipes.md, Agent
executor).
"""

from memini.executors.agent.model import (
    MAX_ATTEMPTS,
    RECIPE_KEY,
    RUN_KIND,
    WINDOW_KEY,
    AgentRun,
    AgentRunClosedError,
    AgentRunOpenError,
    AgentWindow,
    CallMismatchError,
    CallNotHandedOutError,
    FinishResult,
    HandedOutCall,
    InvalidResultError,
    NextStatus,
    NextStep,
    Receipt,
    RecipeChangedError,
    ServerMismatchError,
    SourceNotReadyError,
    WindowOutcome,
    WindowStatus,
    mcp_result,
    source_recipe,
    version_matches,
    window_overrides,
)
from memini.executors.agent.session import (
    AGENT_RUN_DETAIL,
    finish_run,
    get_agent_run,
    next_call,
    start_run,
    submit_response,
)

__all__ = [
    "AGENT_RUN_DETAIL",
    "MAX_ATTEMPTS",
    "RECIPE_KEY",
    "RUN_KIND",
    "WINDOW_KEY",
    "AgentRun",
    "AgentRunClosedError",
    "AgentRunOpenError",
    "AgentWindow",
    "CallMismatchError",
    "CallNotHandedOutError",
    "FinishResult",
    "HandedOutCall",
    "InvalidResultError",
    "NextStatus",
    "NextStep",
    "Receipt",
    "RecipeChangedError",
    "ServerMismatchError",
    "SourceNotReadyError",
    "WindowOutcome",
    "WindowStatus",
    "finish_run",
    "get_agent_run",
    "mcp_result",
    "next_call",
    "source_recipe",
    "start_run",
    "submit_response",
    "version_matches",
    "window_overrides",
]
