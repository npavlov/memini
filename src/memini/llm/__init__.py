"""LLM providers behind one policy gate (principle 8: local by default, cloud as an opt-in).

- :class:`LlmGate` is the one way content reaches an LLM. :meth:`LlmGate.complete` takes the ids
  of every source the content of the request came from, reads the ``llm_policy`` of the
  workspace and of those sources, takes the strictest, and sends the request to a provider that
  policy allows, or refuses (:class:`LlmRefused`) without sending anything. Build it with
  :func:`create_llm_gate`.
- A request asks for structured output: :class:`ChatRequest` names a pydantic model, and the
  reply comes back validated as one (:class:`Completion`), or raises :class:`LlmOutputError`.
- Providers are OpenAI-compatible chat APIs in the settings, each declared ``local`` or
  ``cloud`` by the slot it is set in (``llm_local_*``, ``llm_cloud_*``). The class that sends
  is private to this package; nothing outside it can reach an endpoint but through the gate.
- Token use of every call goes to the run being recorded (:func:`memini.runs.current_run`).
- :func:`set_workspace_llm_policy` changes a workspace's policy, ``cloud`` being its opt-in.

The rules (requirements, section 9, "LLM providers and the policy gate"), by the workspace's
policy and the strictest policy among the sources of the content:

- workspace ``none``, or any source ``none``: refused, nothing is sent.
- workspace ``local``: the local provider; refused when none is configured.
- workspace ``cloud``, a source ``local``: the local provider; refused when none is configured.
- workspace ``cloud``, every source ``cloud``: the cloud provider; the local one when no cloud
  provider is configured; refused when neither is.
"""

from memini.llm.gate import (
    LlmGate,
    ProviderInfo,
    configured_providers,
    create_llm_gate,
    decide,
    strictest,
)
from memini.llm.model import (
    LLM_POLICIES,
    MAX_OUTPUT_TOKENS,
    ChatRequest,
    Completion,
    Grant,
    LlmError,
    LlmOutputError,
    LlmPolicy,
    LlmRefused,
    Message,
    ProviderClass,
    RefusalReason,
    Role,
    Usage,
)
from memini.llm.workspace import get_workspace_llm_policy, set_workspace_llm_policy

__all__ = [
    "LLM_POLICIES",
    "MAX_OUTPUT_TOKENS",
    "ChatRequest",
    "Completion",
    "Grant",
    "LlmError",
    "LlmGate",
    "LlmOutputError",
    "LlmPolicy",
    "LlmRefused",
    "Message",
    "ProviderClass",
    "ProviderInfo",
    "RefusalReason",
    "Role",
    "Usage",
    "configured_providers",
    "create_llm_gate",
    "decide",
    "get_workspace_llm_policy",
    "set_workspace_llm_policy",
    "strictest",
]
