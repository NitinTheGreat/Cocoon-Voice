"""Scripted LIVE probe: a synthetic operator joins a real LiveKit Cloud room with the running worker.

    python -m cocoon_voice.agent dev                       # terminal 1
    python scripts/live_probe.py [--scenarios all]         # terminal 2 (bounded: a few minutes)

Publishes local SAPI speech clips and synthetic noise as the participant's microphone (through LiveKit,
Krisp, VAD, AssemblyAI, Gemini, Cartesia), and records the agent audio as the CLIENT receives it
(after network + jitter buffer; not what a loudspeaker emits). Agent text comes from the room's
lk.transcription stream. Results: metrics/probe-*.json and the received agent audio as a WAV.

This is synthetic speech from one voice; it does not replace human microphone and listening tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
import wave
from datetime import timedelta
from pathlib import Path

import numpy as np
from livekit import api, rtc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cocoon_voice.config import get_settings  # noqa: E402
from cocoon_voice.noise_fixtures import generate  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "audio"
IN_RATE = 16000
OUT_RATE = 24000
SPEAKING_RMS = 250.0
FIXTURE_SET = "sapi"  # sapi (Windows SAPI voice) or natural (scripts/make_natural_fixtures.py, Cartesia)  # int16 RMS above which a received 10 ms agent frame counts as speech


def clip(name: str) -> np.ndarray:
    path = FIX / f"{FIXTURE_SET}_{name}.wav"
    if not path.exists():
        maker = "make_natural_fixtures.py" if FIXTURE_SET == "natural" else "make_speech_fixtures.ps1"
        sys.exit(f"missing fixture {path.name}: run scripts/{maker}")
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).copy()


def noise(kind: str, seconds: float, level_dbfs: float) -> np.ndarray:
    return np.frombuffer(generate(kind, seconds=seconds, sample_rate=IN_RATE, level_dbfs=level_dbfs, seed=11),
                         dtype=np.int16).copy()


class Probe:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.pending: list[np.ndarray] = []
        self.playing_until = 0.0
        self.agent_frames: list[tuple[float, float]] = []  # (t, rms)
        self.agent_pcm = bytearray()
        self.agent_texts: list[tuple[float, str, bool]] = []  # (t, text, final)
        self.events: list[dict] = []
        self.agent_identity: str | None = None

    def now(self) -> float:
        return time.perf_counter() - self.t0

    def mark(self, label: str, **kw) -> None:
        self.events.append({"t": round(self.now(), 3), "event": label, **kw})
        print(f"{self.now():7.2f}s {label} {kw if kw else ''}")

    # ------------------------------------------------------------------ microphone (real-time mixer)

    async def mic(self, source: rtc.AudioSource) -> None:
        step = IN_RATE // 100  # 10 ms
        buf = np.zeros(0, dtype=np.int16)
        next_tick = time.perf_counter()
        while True:
            while self.pending:
                buf = np.concatenate([buf, self.pending.pop(0)])
            chunk = buf[:step]
            buf = buf[step:]
            if len(chunk) < step:
                chunk = np.concatenate([chunk, np.zeros(step - len(chunk), dtype=np.int16)])
            await source.capture_frame(rtc.AudioFrame(chunk.tobytes(), IN_RATE, 1, step))
            next_tick += 0.01
            await asyncio.sleep(max(0.0, next_tick - time.perf_counter()))

    async def play(self, audio: np.ndarray, label: str) -> None:
        self.mark(f"user:{label}:start", dur_s=round(len(audio) / IN_RATE, 2))
        self.pending.append(audio)
        await asyncio.sleep(len(audio) / IN_RATE)
        self.mark(f"user:{label}:end")

    # ------------------------------------------------------------------ agent audio / text

    async def listen(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=OUT_RATE, num_channels=1)
        async for ev in stream:
            data = np.frombuffer(bytes(ev.frame.data.cast("B")), dtype=np.int16)
            rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2))) if len(data) else 0.0
            self.agent_frames.append((self.now(), rms))
            self.agent_pcm += data.tobytes()

    def agent_speaking(self, window_s: float = 0.25) -> bool:
        cutoff = self.now() - window_s
        for t, rms in reversed(self.agent_frames):  # newest first; stop once past the window
            if t < cutoff:
                return False
            if rms > SPEAKING_RMS:
                return True
        return False

    async def wait_speech_start(self, timeout: float) -> float | None:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if self.agent_speaking(0.05):
                return self.now()
            await asyncio.sleep(0.02)
        return None

    async def wait_silence(self, quiet_s: float, timeout: float) -> float | None:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if not self.agent_speaking(quiet_s):
                return self.now() - quiet_s
            await asyncio.sleep(0.05)
        return None

    def speech_segments(self, start: float, end: float, gap_s: float = 0.2) -> list[tuple[float, float]]:
        segs: list[list[float]] = []
        for t, rms in self.agent_frames:
            if t < start or t > end or rms <= SPEAKING_RMS:
                continue
            if segs and t - segs[-1][1] <= gap_s:
                segs[-1][1] = t
            else:
                segs.append([t, t])
        return [(round(a, 2), round(b, 2)) for a, b in segs]

    def texts_between(self, start: float, end: float) -> list[str]:
        return [txt for t, txt, final in self.agent_texts if start <= t <= end and final]


async def run(scenarios: list[str]) -> dict:
    s = get_settings()
    room_name = f"probe-{uuid.uuid4().hex[:6]}"
    token = (api.AccessToken(s.livekit_api_key, s.livekit_api_secret.get_secret_value())
             .with_identity("probe-operator").with_name("probe-operator").with_ttl(timedelta(minutes=20))
             .with_grants(api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True,
                                          can_publish_data=True))
             .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=s.agent_name)]))
             .to_jwt())
    p = Probe()
    room = rtc.Room()
    listen_tasks: list[asyncio.Task] = []

    @room.on("track_subscribed")
    def _on_track(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            p.agent_identity = participant.identity
            p.mark("agent_audio_subscribed", participant=participant.identity)
            listen_tasks.append(asyncio.create_task(p.listen(track)))

    async def read_text(reader, participant_identity: str) -> None:
        try:
            text = await reader.read_all()
        except Exception:
            return  # stream cut by an interruption or disconnect
        if participant_identity != "probe-operator" and text.strip():
            p.agent_texts.append((p.now(), text.strip(), True))

    room.register_text_stream_handler(
        "lk.transcription", lambda reader, pid: asyncio.create_task(read_text(reader, pid)))
    await room.connect(s.livekit_url, token)
    p.mark("connected", room=room_name)
    source = rtc.AudioSource(IN_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("probe-mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    mic_task = asyncio.create_task(p.mic(source))
    results: dict = {"room": room_name, "fixtures": FIXTURE_SET, "scenarios": {}}

    greet = await p.wait_speech_start(30)
    p.mark("greeting_heard" if greet else "NO_GREETING")
    await p.wait_silence(1.0, 15)
    await asyncio.sleep(0.5)

    async def ask_long(wake: bool) -> tuple[float | None, float]:
        ask_end = p.now()
        await p.play(clip("hey_cat_long_request" if wake else "long_request"), "long_request")
        ask_end = p.now()
        start = await p.wait_speech_start(15)
        p.mark("answer_audio_start", latency_after_user_speech_s=round(start - ask_end, 2) if start else None)
        return start, ask_end

    async def settle() -> None:
        await p.wait_silence(2.5, 60)
        await asyncio.sleep(1.0)

    first = True
    for name in scenarios:
        p.mark(f"=== scenario {name}")
        t_start = p.now()
        r: dict = {}
        if name == "baseline":
            start, ask_end = await ask_long(first)
            end = await p.wait_silence(2.5, 60)
            r = {"answer_start_after_user_s": round(start - ask_end, 2) if start else None,
                 "answer_duration_s": round(end - start, 2) if (start and end) else None,
                 "segments": p.speech_segments(start or 0, end or p.now())}
        elif name == "noise":
            start, _ = await ask_long(first)
            await asyncio.sleep(3.0)
            outcomes = []
            for kind, secs, lvl in (("impacts", 1.2, -18.0), ("fan", 2.0, -26.0), ("machinery", 2.0, -22.0)):
                before = p.agent_speaking(0.2)
                await p.play(noise(kind, secs, lvl), f"noise_{kind}")
                resumed_at = await p.wait_speech_start(4.0)
                outcomes.append({"noise": kind, "agent_was_speaking": before,
                                 "agent_audio_within_4s_after": resumed_at is not None})
                await asyncio.sleep(1.5)
            end = await p.wait_silence(2.5, 60)
            r = {"noise_outcomes": outcomes, "segments": p.speech_segments(start or 0, end or p.now())}
        elif name == "backchannel":
            start, _ = await ask_long(first)
            await asyncio.sleep(3.0)
            words = ["mm_hmm", "okay"] + (["yeah", "right"] if FIXTURE_SET == "natural" else [])
            outcomes = {}
            for word in words:
                if not p.agent_speaking(0.5):
                    outcomes[word] = "skipped: agent not speaking"
                    continue
                await p.play(clip(word), word)
                # the worker log's "overlap verdict" line is the authoritative outcome; this only
                # checks the operator kept hearing the agent
                outcomes[word] = "agent audio continued" if await p.wait_speech_start(4.0) else "agent silent"
                await asyncio.sleep(3.0)
            end = await p.wait_silence(2.5, 60)
            r = {"backchannels": outcomes, "segments": p.speech_segments(start or 0, end or p.now())}
        elif name == "stop":
            start, _ = await ask_long(first)
            await asyncio.sleep(3.0)
            await p.play(clip("stop"), "stop")
            stop_end = p.now()
            silent_at = await p.wait_silence(0.4, 5.0)
            later = await p.wait_speech_start(6.0)
            r = {"output_silent_after_stop_s": round(silent_at - stop_end, 2) if silent_at else None,
                 "spoke_again_within_6s": later is not None}
        elif name == "correction":
            start, _ = await ask_long(first)
            await asyncio.sleep(3.0)
            await p.play(clip("okay_but_correction"), "correction")
            corr_end = p.now()
            new_start = await p.wait_speech_start(10.0)
            end = await p.wait_silence(2.5, 60)
            r = {"new_answer_start_after_s": round(new_start - corr_end, 2) if new_start else None,
                 "answer_texts": p.texts_between(corr_end, p.now())}
        elif name == "pause":
            if first:
                await p.play(clip("hey_cat_only"), "hey_cat")
                await settle()
            await p.play(clip("question_part1"), "part1")
            gap_start = p.now()
            early = await p.wait_speech_start(1.0)  # user is "thinking" for 1.0 s
            await p.play(clip("question_part2"), "part2")
            part2_end = p.now()
            reply = await p.wait_speech_start(10.0)
            end = await p.wait_silence(2.5, 60)
            r = {"replied_during_thinking_pause": early is not None and early >= gap_start,
                 "reply_after_part2_s": round(reply - part2_end, 2) if reply else None,
                 "texts": p.texts_between(gap_start, p.now())}
        first = False
        r["agent_texts"] = p.texts_between(t_start, p.now())
        results["scenarios"][name] = r
        p.mark(f"=== {name} result", **{k: v for k, v in r.items() if k not in ("agent_texts", "segments")})
        await settle()

    results["events"] = p.events
    mic_task.cancel()
    for t in listen_tasks:
        t.cancel()
    await room.disconnect()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    s.metrics_dir.mkdir(parents=True, exist_ok=True)
    out = s.metrics_dir / f"probe-{stamp}.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    wav = s.record_audio_dir / f"probe-{stamp}-agent.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(bytes(p.agent_pcm))
    print(f"report: {out}\nagent audio as received: {wav}")
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenarios", default="baseline,noise,backchannel,stop,correction,pause")
    ap.add_argument("--fixtures", choices=("sapi", "natural"), default="sapi")
    args = ap.parse_args()
    global FIXTURE_SET
    FIXTURE_SET = args.fixtures
    asyncio.run(run([x.strip() for x in args.scenarios.split(",") if x.strip()]))


if __name__ == "__main__":
    main()
