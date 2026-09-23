"""Test doubles: scripted streaming LLM, fake session/phrase cache, settings factory. No network."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectionError, APIConnectOptions, llm

from cocoon_voice.config import VoiceSettings
from cocoon_voice.observability import SessionMetrics
from cocoon_voice.phrase_cache import CachedAudio

BASE = {
    "LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s",
    "ASSEMBLYAI_API_KEY": "a", "CARTESIA_API_KEY": "c", "GOOGLE_CLOUD_PROJECT": "orbit-507316",
}


def settings(**overrides) -> VoiceSettings:
    return VoiceSettings(**{**BASE, **overrides}, _env_file=None)


@dataclass
class Step:
    """One scripted chunk: text after `delay` seconds, or raise `error`."""

    text: str = ""
    delay: float = 0.0
    error: Exception | None = None


@dataclass
class Script:
    steps: list[Step]


class FakeLLM(llm.LLM):
    """Streams scripted chunks through the real LLMStream machinery. One script per chat() call."""

    def __init__(self, *scripts: list[Step] | Script):
        super().__init__()
        self.scripts = [s.steps if isinstance(s, Script) else s for s in scripts]
        self.calls = 0
        self.seen_user_texts: list[str] = []
        self.closed_streams = 0

    @property
    def model(self) -> str:
        return "fake-llm"

    @property
    def provider(self) -> str:
        return "tests"

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS, **kwargs: Any) -> llm.LLMStream:
        steps = self.scripts[min(self.calls, len(self.scripts) - 1)] if self.scripts else [Step("ok")]
        self.calls += 1
        users = [i.text_content for i in chat_ctx.items if i.type == "message" and i.role == "user"]
        self.seen_user_texts.append(users[-1] if users else "")
        return _FakeStream(self, steps, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _FakeStream(llm.LLMStream):
    def __init__(self, fake: FakeLLM, steps: list[Step], **kw):
        super().__init__(fake, **kw)
        self._fake = fake
        self._steps = steps

    async def _run(self) -> None:
        try:
            for i, step in enumerate(self._steps):
                if step.delay:
                    await asyncio.sleep(step.delay)
                if step.error is not None:
                    raise step.error
                self._event_ch.send_nowait(llm.ChatChunk(id=f"c{i}", delta=llm.ChoiceDelta(role="assistant",
                                                                                         content=step.text)))
        finally:
            self._fake.closed_streams += 1


def transient_error() -> Exception:
    return APIConnectionError("simulated provider disconnect", retryable=True)


class FakeHandle:
    def __init__(self) -> None:
        self.interrupted = False

    async def wait_for_playout(self) -> None:
        return None


@dataclass
class FakeSession:
    said: list[dict] = field(default_factory=list)
    interrupts: int = 0
    agent_state: str = "listening"
    user_state: str = "listening"

    def say(self, text: str, **kwargs) -> FakeHandle:
        self.said.append({"text": text, **kwargs})
        return FakeHandle()

    def interrupt(self, *, force: bool = False):
        self.interrupts += 1
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut


class FakePhraseCache:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def get(self, text: str) -> CachedAudio:
        self.requested.append(text)
        return CachedAudio(b"\x00\x00" * 480, 24000, 1)

    async def prefetch(self, texts: list[str]) -> None:
        for t in texts:
            await self.get(t)


def metrics() -> SessionMetrics:
    return SessionMetrics(session_label="test", config={}, metrics_dir=None, log_transcripts=False)
