"""Test helpers for :mod:`memini.llm`: a fake OpenAI-compatible chat server and settings.

The fake server answers on every host, so one transport serves both provider slots; each request
it records keeps its URL, so a test can tell which endpoint, local or cloud, received content.
No network. Keys and URLs are obvious fakes.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from pydantic import BaseModel, SecretStr

from memini.config import Settings

LOCAL_URL = "http://llm-local.test:11434/v1"
CLOUD_URL = "https://llm-cloud.test/v1"
LOCAL_HOST = "llm-local.test"
CLOUD_HOST = "llm-cloud.test"
LOCAL_MODEL = "fake-local-model:7b"
CLOUD_MODEL = "fake-cloud-model"
LOCAL_KEY = "sk-fake-local-llm-key-not-real-0001"
CLOUD_KEY = "sk-fake-cloud-llm-key-not-real-0002"
FAKE_DATABASE_URL = "postgresql://fake:fake@db.invalid:5432/memini"

type Respond = Callable[[httpx2.Request], Awaitable[httpx2.Response] | httpx2.Response]


class Verdict(BaseModel):
    """The structured output the tests ask for."""

    answer: str
    confidence: float = 0.0


def support_answer(body: dict[str, Any], *, supported: bool = True) -> httpx2.Response:
    """A verifier reply with a quote from the cited passage for open questions."""
    block = re.search(
        r"^<<<BEGIN DATA [a-f0-9]+>>>\n(.*?)\n<<<END DATA",
        body["messages"][-1]["content"],
        re.M | re.S,
    )
    assert block is not None
    claim, *parts = [json.loads(line) for line in block.group(1).splitlines()]
    reason = "Checked the evidence."
    if claim["claim_type"] == "open_question" and supported:
        reason = next(p["text"] for p in parts if p["id"] in claim["cited"])
    return chat_answer(json.dumps({"reason": reason, "supported": supported}))


def verified_answer(body: dict[str, Any]) -> httpx2.Response | None:
    """A successful evidence check for pipeline tests with predefined extraction results."""
    match body["response_format"]["json_schema"]["name"]:
        case "passage_role":
            return chat_answer('{"purpose": "Ordinary text.", "role": "context"}')
        case "question_act":
            return chat_answer('{"meaning": "An explicit question.", "raises_question": true}')
        case "decision_confirmation":
            return chat_answer('{"reason": "", "supported": false}')
        case "item_act":
            return chat_answer('{"gist": "Settles a choice.", "label": "choice"}')
        case "decision_support":
            return support_answer(body)
        case _:
            return None


def chat_answer(
    content: str | None,
    *,
    finish_reason: str = "stop",
    usage: tuple[int, int] | None = (11, 7),
    refusal: str | None = None,
) -> httpx2.Response:
    """An OpenAI chat completion with one choice."""
    body: dict[str, Any] = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "whatever-the-server-says",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        }
    return httpx2.Response(200, json=body)


@dataclass
class FakeChatServer:
    """``/chat/completions`` on any host. ``script`` answers the first requests in turn; after it
    runs out, every request gets ``reply`` as its content."""

    reply: str = json.dumps({"answer": "yes", "confidence": 0.75})
    script: list[Respond] = field(default_factory=list[Respond])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.script:
            result = self.script.pop(0)(request)
            return await result if isinstance(result, Awaitable) else result
        return chat_answer(self.reply)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handle)

    def hosts(self) -> list[str]:
        """The host of each request, in order."""
        return [request.url.host for request in self.requests]

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def llm_settings(*, local: bool = True, cloud: bool = True, **overrides: Any) -> Settings:
    """Settings with the fake local and cloud providers; neither connects anywhere real."""
    values: dict[str, Any] = {"database_url": SecretStr(FAKE_DATABASE_URL)}
    if local:
        values |= {
            "llm_local_url": LOCAL_URL,
            "llm_local_model": LOCAL_MODEL,
            "llm_local_api_key": SecretStr(LOCAL_KEY),
        }
    if cloud:
        values |= {
            "llm_cloud_url": CLOUD_URL,
            "llm_cloud_model": CLOUD_MODEL,
            "llm_cloud_api_key": SecretStr(CLOUD_KEY),
        }
    values |= overrides
    return Settings.model_validate(values)


class Sleeps:
    """Records the waits between attempts instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
