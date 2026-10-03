"""StackChan voice edge — connects an M5Stack StackChan (stock xiaozhi-based firmware) to an OpenClaw agent.

Speaks the firmware's xiaozhi WebSocket protocol, runs speech locally (SenseVoice STT, Kokoro/VITS TTS),
and hands text turns to the StackChan OpenClaw channel plugin over mutual TLS. The device can only send
audio and listen/abort control; it can never pick an agent, session, model or tool.
Audio lives in memory only; logs carry metadata, never conversation text.
Configuration: edge/config.json (see config.example.json).
"""
import asyncio
import json
import re
import logging
import os
import ssl
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import numpy as np
import opuslib
import sherpa_onnx
from aiohttp import web, ClientSession, ClientTimeout, TCPConnector, WSMsgType

import speech

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
PKI = os.environ.get("STACKCHAN_PKI", os.path.join(HERE, "..", "pki"))


def load_config():
    path = os.environ.get("STACKCHAN_CONFIG", os.path.join(HERE, "config.json"))
    with open(path) as f:
        return json.load(f)


CFG = load_config()
BIND = os.environ.get("EDGE_BIND", CFG["bind"])
PORT = int(os.environ.get("EDGE_PORT", CFG.get("port", 8003)))
DEVICE_MAC = CFG["device_mac"].lower()          # the one device allowed to connect
DEVICE_CLIENT_ID = CFG["device_client_id"]
CHANNEL = os.environ.get("EDGE_CHANNEL", CFG["channel_url"])
# Optional: before this ISO-8601 instant the edge sends display text but no audio (e.g. a household quiet window).
QUIET_UNTIL = datetime.fromisoformat(CFG["quiet_until"]).timestamp() if CFG.get("quiet_until") else 0
# A localhost test instance (unreachable from the device) may emit audio frames for the emulator,
# and its turns go to a throwaway selftest session, never the child's.
TEST_INSTANCE = os.environ.get("EDGE_TEST_INSTANCE") == "1" and BIND == "127.0.0.1"
if TEST_INSTANCE:
    QUIET_UNTIL = 0
ACK_AFTER_S = 1.8
MAX_UTTERANCE_S = 15
LED = {"listening": (0, 140, 0), "thinking": (0, 40, 160), "off": (0, 0, 0)}  # child's cue: green = I'm listening
FOLLOWUP_WINDOW_S = 30  # after 30 s with no speech the conversation ends; the wake word is needed again
END_SILENCE_S = 1.0
IN_RATE, OUT_RATE, FRAME_MS = 16000, 24000, 60
OUT_FRAME = OUT_RATE * FRAME_MS // 1000
PREBUFFER = 5
# Device MCP tools the edge may call. Camera, reboot, firmware upgrade, asset download and screen
# snapshot are never called (and user-only tools are never even listed).
MCP_ALLOWED = {"self.robot.set_head_angles", "self.robot.get_head_angles", "self.robot.set_led_color",
               "self.audio_speaker.set_volume", "self.get_device_status"}
WAKE_RE = re.compile(r"^\s*((hi|hey|hello|嗨|你好|哈喽)[\s,，]*)?(stack|stuck|stak|撕|斯)[\s-]*(chan|chen|tron|酱|将|张)\b[\s,，.。!！]*", re.I)
HEAD_CENTER = (0, 15)  # yaw 0 = facing forward; pitch 15 = slightly raised (robot range yaw ±128, pitch 0-90)
MOTIONS = ["look_left", "look_right", "look_up", "look_down", "look_center", "nod", "shake_head", "volume_up", "volume_down"]
DIRECT_ACK = {"en": "Okay!", "zh": "好的！", "yue": "好呀！"}
JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MIN_P = 0.85
ACK_TEXT = {"en": "Let me think.", "zh": "让我想一想。", "yue": "等我想一想。"}
ERROR_TEXT = {"en": "Sorry, I can't answer right now.", "zh": "对不起，我现在没办法回答。", "yue": "對唔住，我而家答唔到你。"}

log = logging.getLogger("edge")
pool = ThreadPoolExecutor(max_workers=3)


def device_token():
    """Per-device bearer token the robot presents on the WebSocket; created on first use."""
    path = os.path.join(STATE, "device-token")
    if not os.path.exists(path):
        import secrets
        os.makedirs(STATE, mode=0o700, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(secrets.token_urlsafe(24))
    with open(path) as f:
        return f.read().strip()


def to_24k(samples: np.ndarray, sr: int) -> np.ndarray:
    if sr == OUT_RATE:
        return samples
    idx = np.linspace(0, len(samples) - 1, int(len(samples) * OUT_RATE / sr))
    return np.interp(idx, np.arange(len(samples)), samples).astype(np.float32)


def loud_for_small_speaker(x: np.ndarray, hp_hz=250.0, target_rms_db=-13.0, drive=2.5):
    """Louder on the robot. The 1 W speaker can't reproduce bass and perceived loudness follows average level,
    so: 2nd-order high-pass (RBJ biquad), raise RMS to ~-13 dBFS, then a tanh soft limiter instead of clipping."""
    if len(x) == 0:
        return x
    w0 = 2 * np.pi * hp_hz / OUT_RATE
    alpha = np.sin(w0) / (2 * 0.707)
    cw = np.cos(w0)
    b = np.array([(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]); a = np.array([1 + alpha, -2 * cw, 1 - alpha])
    b, a = b / a[0], a / a[0]
    y = np.zeros_like(x, dtype=np.float64)
    x1 = x2 = y1 = y2 = 0.0
    for i, xi in enumerate(x.astype(np.float64)):
        yi = b[0] * xi + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        x2, x1, y2, y1 = x1, xi, y1, yi
        y[i] = yi
    rms = float(np.sqrt(np.mean(y ** 2))) or 1e-9
    y = y * (10 ** (target_rms_db / 20) / rms)
    y = np.tanh(drive * y) / np.tanh(drive)
    return (y * 0.97).astype(np.float32)


def opus_frames(samples24k: np.ndarray):
    enc = opuslib.Encoder(OUT_RATE, 1, opuslib.APPLICATION_AUDIO)
    samples24k = loud_for_small_speaker(samples24k)
    pcm = np.clip(samples24k, -1, 1)
    pcm = (pcm * 32767).astype(np.int16)
    pad = (-len(pcm)) % OUT_FRAME
    if pad:
        pcm = np.concatenate([pcm, np.zeros(pad, np.int16)])
    return [enc.encode(pcm[i:i + OUT_FRAME].tobytes(), OUT_FRAME) for i in range(0, len(pcm), OUT_FRAME)]


class Engine:
    """Shared models, loaded once."""

    def __init__(self):
        t = time.time()
        self.stt = speech.STT()
        self.tts = speech.TTS()
        self.vad_cfg = sherpa_onnx.VadModelConfig()
        self.vad_cfg.silero_vad.model = os.path.join(speech.M, "silero_vad.onnx")
        self.vad_cfg.silero_vad.min_silence_duration = END_SILENCE_S
        self.vad_cfg.silero_vad.threshold = 0.5
        self.vad_cfg.sample_rate = IN_RATE
        self.ack = {lang: opus_frames(to_24k(*self.tts.synth(txt, lang))) for lang, txt in ACK_TEXT.items()}
        self.direct_ack = {lang: opus_frames(to_24k(*self.tts.synth(txt, lang))) for lang, txt in DIRECT_ACK.items()}
        self.jev_key = self.load_jev_key()
        log.info("models loaded in %.1fs", time.time() - t)

    @staticmethod
    def load_jev_key():
        """Jev (TypeSafe) key for intent triage, held in memory only.
        TYPESAFE_API_KEY env, else the shell command in config "jev_key_command"; no key = triage off."""
        import subprocess
        k = os.environ.get("TYPESAFE_API_KEY")
        cmd = CFG.get("jev_key_command")
        if not k and cmd:
            try:
                k = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=40).stdout.strip()
            except Exception as ex:  # noqa: BLE001
                log.warning("jev key command failed: %s", type(ex).__name__)
        log.info("jev triage %s", "enabled" if k else "disabled (no key)")
        return k or None

    def new_vad(self):
        return sherpa_onnx.VoiceActivityDetector(self.vad_cfg, buffer_size_in_seconds=MAX_UTTERANCE_S + 5)


class Session:
    def __init__(self, engine, ws, http, jev_http=None):
        self.e, self.ws, self.http, self.jev_http = engine, ws, http, jev_http
        self.sid = str(uuid.uuid4())
        self.dec = opuslib.Decoder(IN_RATE, 1)
        self.listening = False
        self.mode = "manual"
        self.pcm = []
        self.vad = None
        self.heard_speech = False
        self.gen = 0
        self.turn_task = None
        self.turn_id = None
        self.last_lang = "en"
        self.mcp_id = 0
        self.mcp_wait = {}
        self.tools = {}
        self.home = None  # (yaw, pitch) read from the robot at MCP setup
        self.volume = None
        self.last_activity = time.time()
        self.motion_lock = asyncio.Lock()

    async def send(self, obj):
        obj.setdefault("session_id", self.sid)
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    # ---- inbound -------------------------------------------------------------
    async def on_text(self, msg):
        t = msg.get("type")
        if t == "hello":
            await self.send({"type": "hello", "transport": "websocket",
                             "audio_params": {"format": "opus", "sample_rate": OUT_RATE, "channels": 1, "frame_duration": FRAME_MS}})
            if (msg.get("features") or {}).get("mcp"):
                asyncio.create_task(self.mcp_setup())
        elif t == "listen":
            st = msg.get("state")
            if st == "start":
                await self.interrupt("listen")  # PTT press cancels any playback first
                self.listening, self.mode, self.pcm, self.heard_speech = True, msg.get("mode", "manual"), [], False
                self.vad = self.e.new_vad()
                asyncio.create_task(self.led("listening"))
                log.info("listen start mode=%s", self.mode)
            elif st == "stop" and self.listening:
                self.listening = False
                self.start_turn("ptt" if self.mode == "manual" else "wake")
            elif st == "detect":
                log.info("wake detected on device")
        elif t == "mcp":
            pl = msg.get("payload") or {}
            fut = self.mcp_wait.pop(pl.get("id"), None)
            if fut and not fut.done():
                fut.set_result(pl)
        elif t == "abort":
            await self.interrupt(msg.get("reason") or "button")
        else:
            log.info("ignored message type=%s", t)

    def on_audio(self, data: bytes):
        if not self.listening:
            return
        pcm = np.frombuffer(self.dec.decode(data, IN_RATE * FRAME_MS // 1000), dtype=np.int16).astype(np.float32) / 32768
        self.pcm.append(pcm)
        if sum(len(p) for p in self.pcm) > MAX_UTTERANCE_S * IN_RATE:
            self.listening = False
            self.start_turn("ptt" if self.mode == "manual" else "wake")
            return
        if self.mode != "manual":  # auto mode: server-side end-of-speech
            self.vad.accept_waveform(pcm)
            if self.vad.is_speech_detected():
                self.heard_speech = True
                self.last_activity = time.time()
            elif self.heard_speech and not self.vad.empty():
                self.listening = False
                self.start_turn("wake")

    # ---- device MCP (xiaozhi docs/mcp-protocol.md) -----------------------------
    async def mcp(self, method, params, timeout=5.0):
        self.mcp_id += 1
        mid = self.mcp_id
        fut = asyncio.get_running_loop().create_future()
        self.mcp_wait[mid] = fut
        await self.send({"type": "mcp", "payload": {"jsonrpc": "2.0", "method": method, "params": params, "id": mid}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.mcp_wait.pop(mid, None)

    async def mcp_setup(self):
        try:
            await self.mcp("initialize", {"capabilities": {}})  # no vision capability: camera stays unused
            cursor = ""
            while True:
                r = await self.mcp("tools/list", {"cursor": cursor, "withUserTools": False})
                res = r.get("result") or {}
                for tl in res.get("tools", []):
                    self.tools[tl["name"]] = tl
                cursor = res.get("nextCursor") or ""
                if not cursor:
                    break
            log.info("mcp ready: %d tools, allowed present: %s", len(self.tools), sorted(set(self.tools) & MCP_ALLOWED))
            r = await self.call_tool("self.robot.get_head_angles", {})
            try:
                txt = (r or {}).get("content", [{}])[0].get("text", "")
                ang = json.loads(txt)
                self.home = (int(ang.get("yaw", 0)), int(ang.get("pitch", 45)))
            except Exception:  # noqa: BLE001
                self.home = (0, 45)
            log.info("head home yaw=%s pitch=%s (raw %s)", self.home[0], self.home[1], (r or {}).get("content"))
            st = await self.call_tool("self.get_device_status", {})
            try:
                self.volume = int(json.loads(st["content"][0]["text"])["audio_speaker"]["volume"])
            except Exception:  # noqa: BLE001
                self.volume = 50
            log.info("speaker volume %s", self.volume)
            tgt = os.path.join(STATE, "volume-target")  # one-shot operator request, consumed on next connect
            if os.path.exists(tgt):
                try:
                    self.volume = max(20, min(100, int(open(tgt).read().strip())))
                    await self.call_tool("self.audio_speaker.set_volume", {"volume": self.volume})
                    log.info("speaker volume set to %d (operator request)", self.volume)
                finally:
                    os.remove(tgt)
            await self.led("listening" if self.listening else "off")
            for name in ("self.robot.set_head_angles", "self.robot.set_led_color"):
                if name in self.tools:
                    log.info("mcp schema %s: %s", name, json.dumps(self.tools[name].get("inputSchema"))[:600])
        except Exception as ex:  # noqa: BLE001
            log.warning("mcp setup failed: %s", type(ex).__name__)

    async def call_tool(self, name, args):
        if name not in MCP_ALLOWED or name not in self.tools:
            log.warning("mcp call refused: %s", name)
            return None
        try:
            r = await self.mcp("tools/call", {"name": name, "arguments": args})
            return r.get("result")
        except Exception as ex:  # noqa: BLE001
            log.warning("mcp call %s failed: %s", name, type(ex).__name__)
            return None

    async def move(self, symbol):
        """Bounded head motion from a fixed symbol; the firmware enforces its own limits on top."""
        if symbol in ("volume_up", "volume_down"):
            self.volume = max(20, min(100, (self.volume or 50) + (20 if symbol == "volume_up" else -20)))
            await self.call_tool("self.audio_speaker.set_volume", {"volume": self.volume})
            log.info("volume -> %d", self.volume)
            return
        if symbol not in MOTIONS or "self.robot.set_head_angles" not in self.tools:
            return
        y0, p0 = HEAD_CENTER
        clamp_y = lambda v: max(-90, min(90, v))
        clamp_p = lambda v: max(5, min(85, v))
        steps = {
            "look_left": [(y0 + 40, p0)], "look_right": [(y0 - 40, p0)],
            "look_up": [(y0, p0 + 20)], "look_down": [(y0, p0 - 15)], "look_center": [(y0, p0)],
            "nod": [(y0, p0 - 12), (y0, p0 + 4), (y0, p0 - 12), (y0, p0)],
            "shake_head": [(y0 + 20, p0), (y0 - 20, p0), (y0 + 20, p0), (y0, p0)],
        }[symbol]
        async with self.motion_lock:
            for y, p in steps:
                await self.call_tool("self.robot.set_head_angles", {"yaw": clamp_y(y), "pitch": clamp_p(p), "speed": 400})
                await asyncio.sleep(0.35)
        log.info("motion %s done", symbol)

    async def led(self, state):
        if "self.robot.set_led_color" in self.tools:
            r, g, b = LED[state]
            await self.call_tool("self.robot.set_led_color", {"red": r, "green": g, "blue": b})

    async def move_tilt(self):
        """Thinking pose: a small head tilt while the agent works (mood move)."""
        if "self.robot.set_head_angles" in self.tools:
            y0, p0 = HEAD_CENTER
            async with self.motion_lock:
                await self.call_tool("self.robot.set_head_angles", {"yaw": y0 + 8, "pitch": max(5, min(85, p0 + 8)), "speed": 200})

    async def jev_triage(self, text):
        """Is this only a request to move the robot's head? Returns a motion symbol or None."""
        if not self.e.jev_key:
            return None
        crit = {"conversation": "Anything that needs a spoken answer: a question, chat, a story, feelings, or a request that mixes moving with talking.",
                "look_left": "Only asks the robot to look or turn to the left.", "look_right": "Only asks the robot to look or turn to the right.",
                "look_up": "Only asks the robot to look up.", "look_down": "Only asks the robot to look down.",
                "look_center": "Only asks the robot to look straight ahead / back to the middle / at me.",
                "nod": "Only asks the robot to nod (yes).", "shake_head": "Only asks the robot to shake its head (no).",
                "volume_up": "Only asks the robot to speak louder / turn the volume up.", "volume_down": "Only asks the robot to be quieter / turn the volume down."}
        body = {"model": "jev-latest", "state": {"child_said_to_her_desk_robot": text},
                "questions": {"intent": {"type": "choice", "instructions": "A child spoke to her desk robot. Is this ONLY a command to move the robot's head or change its volume, with nothing to answer? Pick that action, otherwise conversation.", "criteria": crit}}}
        try:
            async with self.jev_http.post(JEV_URL, json=body, headers={"Authorization": f"Bearer {self.e.jev_key}"}, timeout=ClientTimeout(total=2.5)) as r:
                a = (await r.json())["answers"]["intent"]
            c, p = a["choice"], (a.get("probabilities") or {}).get(a["choice"], 0)
            log.info("jev triage -> %s p=%.2f", c, p)
            return c if c != "conversation" and p >= JEV_MIN_P else None
        except Exception as ex:  # noqa: BLE001
            log.warning("jev triage failed: %s", type(ex).__name__)
            return None

    # ---- turn ----------------------------------------------------------------
    async def followup_guard(self):
        """Close the audio session once the follow-up window passes with nobody speaking: the device then
        drops back to wake-word standby and stops streaming room audio."""
        while not self.ws.closed:
            await asyncio.sleep(2)
            busy = self.turn_task is not None and not self.turn_task.done()
            if busy:
                self.last_activity = time.time()
            elif self.listening and time.time() - self.last_activity > FOLLOWUP_WINDOW_S:
                log.info("follow-up window over (%ds silence); closing session", FOLLOWUP_WINDOW_S)
                await self.led("off")
                await self.ws.close()
                return

    def start_turn(self, interaction):
        audio = np.concatenate(self.pcm) if self.pcm else np.zeros(0, np.float32)
        self.pcm = []  # audio is released as soon as it is handed to STT
        self.gen += 1
        self.turn_task = asyncio.create_task(self.run_turn(audio, interaction, self.gen))

    def rearm(self, interaction):
        """Nothing worth answering. In hands-free (auto) mode the device is still streaming, so keep listening
        here too; otherwise the edge drops its audio and the device never hears the wake word again."""
        if interaction == "ptt":
            asyncio.create_task(self.send({"type": "tts", "state": "stop"}))
        else:
            self.listening, self.pcm, self.heard_speech = True, [], False
            self.vad = self.e.new_vad()
            asyncio.create_task(self.led("listening"))

    async def run_turn(self, audio, interaction, gen):
        loop = asyncio.get_running_loop()
        t0 = time.time()
        turn_id = self.turn_id = str(uuid.uuid4())
        if len(audio) < IN_RATE * 0.3:
            self.rearm(interaction)
            return
        text, lang = await loop.run_in_executor(pool, self.e.stt.transcribe, audio)
        text = WAKE_RE.sub("", text).strip(" ,，.。!！")  # the wake phrase is the robot's doorbell, not part of the question
        lang = speech.guess_lang(text) if text else lang
        del audio
        t_stt = time.time()
        if len(text.strip(" .。!?！？,，、嗯啊呃")) < 2:  # noise / a lone syllable / just the wake phrase: never a turn for the agent
            log.info("turn %s ignored short transcript (%d chars)", turn_id[:8], len(text))
            self.rearm(interaction)
            return
        self.last_lang = lang
        asyncio.create_task(self.led("thinking"))  # blue only once there are real words to think about
        await self.send({"type": "stt", "text": text})
        await self.send({"type": "llm", "emotion": "thinking", "text": "🤔"})
        req = asyncio.create_task(self.http.post(f"{CHANNEL}/v1/utterance", json={
            "turn_id": turn_id, "transcript": text, "language": lang if lang in ("en", "zh", "yue") else "en",
            "interaction": "selftest" if TEST_INSTANCE else interaction}))
        triage = asyncio.create_task(self.jev_triage(text))
        direct = await triage
        if direct and gen == self.gen:  # a pure body command: do it now, skip the conversation
            req.cancel()
            try:
                await self.http.post(f"{CHANNEL}/v1/interrupt", json={"turn_id": turn_id})
            except Exception:  # noqa: BLE001
                pass
            await self.send({"type": "llm", "emotion": "happy", "text": "😊"})
            await self.led("off")
            await self.send({"type": "tts", "state": "start"})
            await asyncio.gather(self.play(self.e.direct_ack.get(lang, self.e.direct_ack["en"]), gen), self.move(direct))
            await asyncio.sleep((PREBUFFER + 2) * FRAME_MS / 1000)
            await self.send({"type": "tts", "state": "stop"})
            log.info("turn %s direct motion %s total=%.2fs", turn_id[:8], direct, time.time() - t0)
            return
        asyncio.create_task(self.move_tilt())
        acked = False
        done, _ = await asyncio.wait({req}, timeout=max(0.1, ACK_AFTER_S - (time.time() - t_stt)))
        if not done and gen == self.gen:
            acked = True
            await self.send({"type": "tts", "state": "start"})
            await self.play(self.e.ack[lang if lang in self.e.ack else "en"], gen)
        try:
            resp = await req
            out = await resp.json()
        except Exception as ex:  # noqa: BLE001
            out = {"status": "failed", "code": type(ex).__name__}
        if gen != self.gen:
            log.info("turn %s superseded", turn_id[:8])
            return
        t_ans = time.time()
        answer = out.get("text") if out.get("status") == "completed" else None
        if not answer:
            log.warning("turn %s failed status=%s code=%s", turn_id[:8], out.get("status"), out.get("code"))
            await self.say_text_only(ERROR_TEXT.get(lang, ERROR_TEXT["en"]), "sad", acked)
            return
        reply_lang = speech.guess_lang(answer, lang)
        motions = [m for m in (out.get("motions") or []) if m in MOTIONS][:4]
        if motions:
            async def run_motions():
                for m in motions:
                    await self.move(m)
            asyncio.create_task(run_motions())
        else:
            asyncio.create_task(self.move("look_center"))
        await self.send({"type": "llm", "emotion": "happy", "text": "😊"})
        if not acked:
            await self.send({"type": "tts", "state": "start"})
        await self.led("off")
        first_audio = await self.speak(answer, reply_lang, gen)
        log.info("turn %s ok lang=%s->%s stt=%.2fs agent=%.2fs first_audio=%s total=%.2fs in_len=%d out_len=%d ack=%s",
                 turn_id[:8], lang, reply_lang, t_stt - t0, t_ans - t_stt,
                 f"{first_audio - t0:.2f}s" if first_audio else "-", time.time() - t0, len(text), len(answer), acked)

    async def say_text_only(self, text, emotion, started):
        await self.send({"type": "llm", "emotion": emotion, "text": "😢"})
        if not started:
            await self.send({"type": "tts", "state": "start"})
        await self.send({"type": "tts", "state": "sentence_start", "text": text})
        await asyncio.sleep(2)
        await self.send({"type": "tts", "state": "stop"})

    async def speak(self, answer, lang, gen):
        """Synthesize sentence by sentence (next one renders while the current one plays)."""
        loop = asyncio.get_running_loop()
        parts = speech.sentences(answer)
        first_audio = None

        def render(s):
            a, sr = self.e.tts.synth(s, lang)
            return opus_frames(to_24k(a, sr))
        nxt = loop.run_in_executor(pool, render, parts[0]) if parts else None
        for i, s in enumerate(parts):
            frames = await nxt
            nxt = loop.run_in_executor(pool, render, parts[i + 1]) if i + 1 < len(parts) else None
            if gen != self.gen:
                return first_audio
            await self.send({"type": "tts", "state": "sentence_start", "text": s})
            first_audio = first_audio or time.time()
            await self.play(frames, gen)
        if gen == self.gen:
            await asyncio.sleep((PREBUFFER + 2) * FRAME_MS / 1000)
            await self.send({"type": "tts", "state": "stop"})
        self.last_activity = time.time()
        return first_audio

    async def play(self, frames, gen):
        if time.time() < QUIET_UNTIL:  # quiet hours: display text only, never audio
            await asyncio.sleep(len(frames) * FRAME_MS / 1000)
            return
        start = time.monotonic()
        for n, fr in enumerate(frames):
            if gen != self.gen:
                return
            due = start + max(0, n - PREBUFFER) * FRAME_MS / 1000
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            await self.ws.send_bytes(fr)

    async def interrupt(self, cause):
        if self.turn_task and not self.turn_task.done():
            self.gen += 1
            self.turn_task.cancel()
            await self.send({"type": "tts", "state": "stop"})
            if self.turn_id:
                try:
                    await self.http.post(f"{CHANNEL}/v1/interrupt", json={"turn_id": self.turn_id})
                except Exception:  # noqa: BLE001
                    pass
            log.info("interrupted cause=%s", cause)


def authorized(req, need_token):
    if (req.headers.get("Device-Id", "").lower() != DEVICE_MAC or req.headers.get("Client-Id", "") != DEVICE_CLIENT_ID):
        return False
    if need_token and req.headers.get("Authorization", "") != f"Bearer {device_token()}":
        return False
    return True


async def ota(req):
    if not authorized(req, need_token=False):
        log.warning("ota rejected from %s", req.remote)
        return web.Response(status=403)
    body = {}
    if req.can_read_body:
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
    version = (body.get("application") or {}).get("version") or body.get("version") or "1.5.1"
    log.info("ota check from device fw=%s", version)
    return web.json_response({
        "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": 480},
        "firmware": {"version": str(version), "url": ""},
        "websocket": {"url": f"ws://{BIND}:{PORT}/xiaozhi/v1/", "token": device_token()},
    })


async def ws_handler(req):
    if not authorized(req, need_token=True):
        log.warning("ws rejected from %s", req.remote)
        return web.Response(status=403)
    ws = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=30)
    await ws.prepare(req)
    sess = Session(req.app["engine"], ws, req.app["http"], req.app["jev_http"])
    req.app["sessions"].add(sess)
    log.info("device connected from %s", req.remote)
    guard = asyncio.create_task(sess.followup_guard())
    try:
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                try:
                    obj = json.loads(m.data)
                except ValueError:
                    continue
                if isinstance(obj, dict) and len(m.data) <= 8192:
                    await sess.on_text(obj)
            elif m.type == WSMsgType.BINARY:
                sess.on_audio(m.data)
    finally:
        req.app["sessions"].discard(sess)
        guard.cancel()
        sess.gen += 1
        if sess.turn_task:
            sess.turn_task.cancel()
        log.info("device disconnected")
    return ws


async def make_app():
    app = web.Application()
    app["engine"] = Engine()
    ctx = ssl.create_default_context(cafile=f"{PKI}/ca.crt")
    ctx.load_cert_chain(f"{PKI}/edge.crt", f"{PKI}/edge.key")
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    app["http"] = ClientSession(connector=TCPConnector(ssl=ctx), timeout=ClientTimeout(total=100))
    app["jev_http"] = ClientSession(timeout=ClientTimeout(total=3))
    app["sessions"] = set()

    async def lights_off(a):
        """Never leave the 'listening' green on when the edge goes away."""
        for sess in list(a["sessions"]):
            try:
                await asyncio.wait_for(sess.led("off"), 2)
                await sess.ws.close()
            except Exception:  # noqa: BLE001
                pass
    app.on_shutdown.append(lights_off)
    app.router.add_route("*", "/xiaozhi/ota/", ota)
    app.router.add_get("/xiaozhi/v1/", ws_handler)

    async def close(a):
        await a["http"].close()
        await a["jev_http"].close()
    app.on_cleanup.append(close)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    web.run_app(make_app(), host=BIND, port=PORT, access_log=None)
