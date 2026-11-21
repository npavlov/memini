"""What a caller hands the gate and what it gets back: messages, a request for structured output,
the grant the gate decided on, the completion, and the errors.

A request always asks for structured output: a pydantic model the reply must validate against.
Nothing in a request names a provider; the gate picks one from the policies of the workspace and
of the sources the content came from (:mod:`memini.llm.gate`).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel

__all__ = [
    "LLM_POLICIES",
    "MAX_OUTPUT_TOKENS",
    "ChatRequest",
    "Completion",
    "Grant",
    "LlmError",
    "LlmOutputError",
    "LlmPolicy",
    "LlmRefused",
    "Message",
    "ProviderClass",
    "RefusalReason",
    "Role",
    "Usage",
]

LlmPolicy = Literal["none", "local", "cloud"]
"""Which LLM may read content: none at all, the local provider, or the cloud provider too. The
values of ``source.llm_policy`` and ``workspace.llm_policy``."""

LLM_POLICIES: Final[tuple[LlmPolicy, ...]] = ("none", "local", "cloud")
"""Every policy, strictest first: the order the gate takes the minimum in."""

ProviderClass = Literal["local", "cloud"]
"""What the settings declare an endpoint to be: one trusted with every source's content, or one
trusted only with sources whose policy is ``cloud``, in workspaces that opted in."""

Role = Literal["system", "user", "assistant"]

MAX_OUTPUT_TOKENS: Final = 131072

_OUTPUT_NAME: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


class LlmError(Exception):
    """An LLM call failed, or the reply was unusable.

    The text names the provider class, the endpoint as ``scheme://host:port`` and the model. It
    never holds an API key or the content of a message.
    """


RefusalReason = Literal[
    "no_sources",
    "unknown_workspace",
    "unknown_source",
    "unknown_policy",
    "policy_none",
    "no_provider",
    "grant_mismatch",
]
"""Why the gate refused:

- ``no_sources``: the request named no source; content always comes from somewhere.
- ``unknown_workspace``, ``unknown_source``: no such workspace, or a source that is not in it.
- ``unknown_policy``: a policy value the gate does not know.
- ``policy_none``: the workspace or a source has ``llm_policy`` ``none``. Expected: skip the work.
- ``no_provider``: no provider the policies allow is configured. Expected when no LLM is set up.
- ``grant_mismatch``: an endpoint was handed a grant that is not for it or does not allow its
  class. The gate never does that; the endpoint checks anyway.
"""


class LlmRefused(LlmError):
    """The gate allows no LLM for this content. Nothing was sent anywhere.

    ``reason`` says why (:data:`RefusalReason`); ``policy_none`` and ``no_provider`` are the
    expected ones, which a caller turns into a skip. The text names workspaces, sources and
    policies, never content.
    """

    def __init__(self, reason: RefusalReason, text: str) -> None:
        super().__init__(text)
        self.reason: RefusalReason = reason


class LlmOutputError(LlmError):
    """The model answered, but not with a JSON object that matches the requested schema.

    The text names the fields that failed and how, never their values. The tokens were used, and
    are in the run report.
    """


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ChatRequest[T: BaseModel]:
    """One chat completion whose reply must be a JSON object that validates as ``output``.

    ``output_name`` names the schema in ``response_format`` (letters, digits, ``_``, ``-``).
    ``temperature`` 0 by default: extraction wants the same answer for the same input.
    """

    messages: Sequence[Message]
    output: type[T]
    temperature: float = 0.0
    max_tokens: int = 1024
    output_name: str = "output"

    def __post_init__(self) -> None:
        if isinstance(self.messages, str) or not self.messages:
            raise ValueError("a chat request needs at least one message")
        object.__setattr__(self, "messages", tuple(self.messages))
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature is from 0 to 2")
        if not 1 <= self.max_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError(f"max_tokens is from 1 to {MAX_OUTPUT_TOKENS}")
        if _OUTPUT_NAME.fullmatch(self.output_name) is None:
            raise ValueError("output_name is 1 to 64 letters, digits, '_' or '-'")


@dataclass(frozen=True, slots=True)
class Grant:
    """What the gate decided for one piece of content: which provider may read it.

    ``policy`` is the effective policy, the strictest of the workspace's and of every source's;
    it is never ``none``, which gives no grant. The endpoint checks the grant again before it
    sends anything: a cloud endpoint takes only a grant whose policy is ``cloud``.
    """

    workspace_id: UUID
    source_ids: frozenset[UUID]
    policy: Literal["local", "cloud"]
    provider_class: ProviderClass
    model: str
    target: str
    """``scheme://host:port`` of the endpoint: no path, no credentials."""

    def describe(self) -> str:
        """``the cloud provider (model M at https://host:443)``, for messages and logs."""
        return f"the {self.provider_class} provider (model {self.model} at {self.target})"


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts reported by the endpoint. Unreported counts are zero."""

    input_tokens: int
    output_tokens: int

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True, slots=True)
class Completion[T: BaseModel]:
    output: T
    """The reply, validated."""
    usage: Usage
    grant: Grant = field(repr=False)
    """Which provider and model answered: what an extractor version records."""
