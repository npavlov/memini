"""The policy gate: the one way content reaches an LLM (principle 8).

Every LLM call goes through :meth:`LlmGate.complete`, which takes the ids of the sources the
content of the request came from. Before anything is sent, the gate reads, in the caller's
connection, the ``llm_policy`` of the workspace and of each of those sources, and takes the
strictest (``none`` < ``local`` < ``cloud``):

- ``none``: no LLM. :class:`~memini.llm.LlmRefused` with reason ``policy_none``.
- ``local``: the local provider, if one is configured. Never the cloud provider, whatever the
  workspace setting: a source that is ``local`` keeps a workspace that opted in to cloud local,
  and a workspace that is ``local`` keeps every source in it local.
- ``cloud``: the workspace opted in and every source allows it. The cloud provider if one is
  configured, else the local provider: what may go to the cloud may also stay on the machine.

It fails closed: no source named, a workspace or source that does not exist (or a source of
another workspace), a policy value it does not know, no provider the policy allows: each is a
refusal, and nothing is sent. Content mixed from several sources (a supersession check comparing
items of two sources) is held to the strictest of them, because the request names all of them.

The policies are read at every call, so a change takes effect at the next call, also inside a run
that is under way. :meth:`LlmGate.check` makes the same decision without a call, for a caller that
wants to skip work early or record the model in an extractor version.

Which providers exist is in the settings (:mod:`memini.config`, ``llm_local_*`` and
``llm_cloud_*``); :func:`create_llm_gate` builds the gate from them. The endpoints stay inside the
gate: the class that sends (:mod:`memini.llm._endpoint`) is private to this package, and checks
the gate's :class:`~memini.llm.Grant` again before it sends.
"""

import logging
import random
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Literal, Self
from uuid import UUID

import anyio
import httpx2
from psycopg import AsyncConnection
from pydantic import BaseModel, SecretStr

from memini.config import LlmStructuredOutput, Settings
from memini.llm._endpoint import (
    _GATE_KEY,  # pyright: ignore[reportPrivateUsage] - the gate is the one holder of the key
    ChatEndpoint,
    endpoint_target,
)
from memini.llm.model import (
    LLM_POLICIES,
    ChatRequest,
    Completion,
    Grant,
    LlmError,
    LlmPolicy,
    LlmRefused,
    ProviderClass,
)

__all__ = [
    "LlmGate",
    "ProviderInfo",
    "configured_providers",
    "create_llm_gate",
    "decide",
    "strictest",
]

log = logging.getLogger(__name__)

_PROVIDER_SETTINGS: Final[Mapping[ProviderClass, str]] = {
    "local": "MEMINI_LLM_LOCAL_URL and MEMINI_LLM_LOCAL_MODEL",
    "cloud": "MEMINI_LLM_CLOUD_URL and MEMINI_LLM_CLOUD_MODEL",
}


# --- The decision --------------------------------------------------------------------------------


def strictest(policies: Iterable[str]) -> LlmPolicy:
    """The strictest of ``policies``: ``none`` before ``local`` before ``cloud``.

    Raises:
        LlmRefused: ``unknown_policy`` for a value that is not a policy, or ``no_sources`` for
            none at all.
    """
    ranks: list[int] = []
    for policy in policies:
        if policy not in LLM_POLICIES:
            raise LlmRefused("unknown_policy", "an llm_policy is not one of none, local, cloud")
        ranks.append(LLM_POLICIES.index(policy))
    if not ranks:
        raise LlmRefused("no_sources", "no policy to decide by")
    return LLM_POLICIES[min(ranks)]


def decide(
    workspace: tuple[str, str],
    sources: Iterable[tuple[str, str]],
    configured: Collection[ProviderClass],
) -> tuple[Literal["local", "cloud"], ProviderClass]:
    """The effective policy for content of ``sources`` in ``workspace``, and the provider class
    that may read it. Pure: the rules of the gate without the database.

    Args:
        workspace: the workspace's label for messages (``workspace alpha``) and its policy.
        sources: each source the content came from: its label (``source notes``) and its
            policy. At least one. Pairs, not a mapping, so no two sources can merge into one.
        configured: the provider classes the settings configure.

    Raises:
        LlmRefused: no sources, an unknown policy, a policy ``none``, or no provider allowed.
    """
    everyone = [workspace, *sources]
    if len(everyone) == 1:
        raise LlmRefused(
            "no_sources", "an LLM request names the sources of its content; none was named"
        )
    unknown = [who for who, policy in everyone if policy not in LLM_POLICIES]
    if unknown:
        raise LlmRefused(
            "unknown_policy",
            f"{', '.join(unknown)}: llm_policy is not one of none, local, cloud; no LLM may read"
            " the content",
        )
    effective = strictest(policy for _, policy in everyone)
    limiting = ", ".join(who for who, policy in everyone if policy == effective)
    if effective == "none":
        raise LlmRefused(
            "policy_none", f"no LLM may read this content: {limiting}: llm_policy none"
        )
    if effective == "cloud" and "cloud" in configured:
        return effective, "cloud"
    if "local" in configured:
        return effective, "local"
    if effective == "local":
        unused = "; the cloud provider may not read it" if "cloud" in configured else ""
        raise LlmRefused(
            "no_provider",
            f"the content may go to a local provider only ({limiting}: llm_policy local), and"
            f" none is configured: set {_PROVIDER_SETTINGS['local']}{unused}",
        )
    raise LlmRefused(
        "no_provider",
        "no LLM provider is configured: set"
        f" {_PROVIDER_SETTINGS['local']}, or {_PROVIDER_SETTINGS['cloud']}",
    )


# --- Providers -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """A configured provider, for messages: no key, no URL path."""

    provider_class: ProviderClass
    model: str
    target: str
    """``scheme://host:port``."""

    def describe(self) -> str:
        return f"model {self.model} at {self.target}"


def configured_providers(settings: Settings) -> dict[ProviderClass, ProviderInfo]:
    """The providers the settings configure, by class; without creating a client."""
    found: dict[ProviderClass, ProviderInfo] = {}
    for slot in _slots(settings):
        if slot.url is not None and slot.model is not None:
            found[slot.provider_class] = ProviderInfo(
                slot.provider_class, slot.model, endpoint_target(slot.url)
            )
    return found


@dataclass(frozen=True, slots=True)
class _Slot:
    provider_class: ProviderClass
    url: str | None
    model: str | None
    api_key: SecretStr | None
    structured_output: LlmStructuredOutput


def _slots(settings: Settings) -> tuple[_Slot, _Slot]:
    """The two provider slots of the settings; the class of each is the slot it is set in."""
    return (
        _Slot(
            "local",
            settings.llm_local_url,
            settings.llm_local_model,
            settings.llm_local_api_key,
            settings.llm_local_structured_output,
        ),
        _Slot(
            "cloud",
            settings.llm_cloud_url,
            settings.llm_cloud_model,
            settings.llm_cloud_api_key,
            settings.llm_cloud_structured_output,
        ),
    )


# --- The gate ------------------------------------------------------------------------------------

_SCOPE: Final = """
SELECT w.name, w.llm_policy, s.id, s.name, s.llm_policy
FROM workspace w
LEFT JOIN source s ON s.workspace_id = w.id AND s.id = ANY(%(sources)s)
WHERE w.id = %(workspace)s
"""


class LlmGate:
    """The one way to an LLM. Build it with :func:`create_llm_gate`; close it with
    :meth:`aclose` or ``async with``."""

    def __init__(self, endpoints: Mapping[ProviderClass, ChatEndpoint]) -> None:
        for provider_class, endpoint in endpoints.items():
            # A cloud endpoint filed as local would read every source: refuse to start instead.
            if endpoint.provider_class != provider_class:
                raise ValueError("an LLM endpoint is filed under a class that is not its own")
        self._endpoints: dict[ProviderClass, ChatEndpoint] = dict(endpoints)

    @property
    def providers(self) -> dict[ProviderClass, ProviderInfo]:
        """The configured providers, by class: model and ``scheme://host:port``."""
        return {
            provider_class: ProviderInfo(provider_class, endpoint.model, endpoint.target)
            for provider_class, endpoint in self._endpoints.items()
        }

    async def check(
        self,
        conn: AsyncConnection[Any],
        *,
        workspace_id: UUID,
        source_ids: Iterable[UUID],
    ) -> Grant:
        """The grant the gate would issue now for content of ``source_ids`` in ``workspace_id``.

        Reads the policies in ``conn``; sends nothing. :meth:`complete` decides again on its own.

        Raises:
            LlmRefused: no LLM may read the content, or none that may is configured.
        """
        if isinstance(source_ids, str | UUID):
            raise TypeError("source_ids is a collection of source ids, not one id")
        ids = frozenset(source_ids)
        if not ids:
            raise LlmRefused(
                "no_sources", "an LLM request names the sources of its content; none was named"
            )
        cur = await conn.execute(_SCOPE, {"workspace": workspace_id, "sources": sorted(ids)})
        rows = await cur.fetchall()
        if not rows:
            raise LlmRefused("unknown_workspace", f"no workspace {workspace_id}")
        workspace_name: str = rows[0][0]
        workspace_policy: str = rows[0][1]
        found: dict[UUID, tuple[str, str]] = {
            row[2]: (row[3], row[4]) for row in rows if row[2] is not None
        }
        missing = sorted(str(source_id) for source_id in ids - found.keys())
        if missing:
            raise LlmRefused(
                "unknown_source",
                f"workspace {workspace_name} has no source {', '.join(missing)}; no LLM may read"
                " content of unknown origin",
            )
        policy, provider_class = decide(
            (f"workspace {workspace_name}", workspace_policy),
            [(f"source {name}", source_policy) for name, source_policy in found.values()],
            self._endpoints.keys(),
        )
        endpoint = self._endpoints[provider_class]
        return Grant(
            workspace_id=workspace_id,
            source_ids=ids,
            policy=policy,
            provider_class=provider_class,
            model=endpoint.model,
            target=endpoint.target,
        )

    async def complete[T: BaseModel](
        self,
        conn: AsyncConnection[Any],
        *,
        workspace_id: UUID,
        source_ids: Iterable[UUID],
        request: ChatRequest[T],
    ) -> Completion[T]:
        """Send ``request`` to the provider the policies allow, and return the validated reply.

        ``source_ids`` names every source whose content is in ``request``: all of them, or the
        decision is made on too little. The policies are read in ``conn`` before anything is sent.

        Raises:
            LlmRefused: no LLM may read the content, or none that may is configured. Nothing was
                sent.
            LlmError: the provider failed or refused the request.
            LlmOutputError: the reply does not match ``request.output``.
        """
        grant = await self.check(conn, workspace_id=workspace_id, source_ids=source_ids)
        log.debug(
            "llm: content of %d sources of workspace %s goes to %s",
            len(grant.source_ids),
            workspace_id,
            grant.describe(),
        )
        return await self._endpoints[grant.provider_class].complete(grant, request)

    async def aclose(self) -> None:
        for endpoint in self._endpoints.values():
            await endpoint.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


def create_llm_gate(
    settings: Settings,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    rng: random.Random | None = None,
) -> LlmGate:
    """The gate with the providers the settings configure; none configured gives a gate that
    refuses every request (``no_provider``). Cheap: connections open on the first request.

    API keys are registered with :func:`memini.logs.register_secret`, so logs mask them.
    ``transport``, ``sleep`` and ``rng`` are for tests: an in-process server for every endpoint,
    no real waits, fixed jitter.

    Raises:
        LlmError: a provider's URL is set without its model (settings built by hand only;
            :func:`memini.config.load_settings` refuses that).
    """
    endpoints: dict[ProviderClass, ChatEndpoint] = {}
    for slot in _slots(settings):
        if slot.url is None:
            continue
        name = slot.provider_class
        if slot.model is None:
            raise LlmError(
                f"llm_{name}_url is set, but llm_{name}_model is not:"
                f" set MEMINI_LLM_{name.upper()}_MODEL"
            )
        endpoints[name] = ChatEndpoint(
            _GATE_KEY,
            name,
            url=slot.url,
            model=slot.model,
            api_key=slot.api_key,
            structured_output=slot.structured_output,
            timeout=settings.llm_timeout,
            transport=transport,
            sleep=sleep,
            rng=rng,
        )
    return LlmGate(endpoints)
