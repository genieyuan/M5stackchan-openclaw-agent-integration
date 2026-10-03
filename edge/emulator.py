"""Silent device emulator: speaks the xiaozhi protocol to the edge using synthesized test utterances,
then transcribes the audio the edge sends back. Nothing is played through any speaker."""
import asyncio, json, sys, time, os
import numpy as np, opuslib, aiohttp
import speech

URL = os.environ.get("EMU_URL", "http://127.0.0.1:8013")
_CFG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
HDR = {"Device-Id": _CFG["device_mac"], "Client-Id": _CFG["device_client_id"], "Protocol-Version": "1"}

def to16k(a, sr):
    idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / sr)); return np.interp(idx, np.arange(len(a)), a)

async def main(utterances, mode):
    tts, stt = speech.TTS(), speech.STT()
    async with aiohttp.ClientSession() as http:
        r = await http.post(f"{URL}/xiaozhi/ota/", headers=HDR, json={"application": {"version": "1.5.1"}})
        cfg = await r.json(); tok = cfg["websocket"]["token"]
        ws_url = "ws://" + URL.split("//")[1] + "/xiaozhi/v1/"
        async with http.ws_connect(ws_url, headers={**HDR, "Authorization": f"Bearer {tok}"}) as ws:
            await ws.send_str(json.dumps({"type": "hello", "version": 1, "transport": "websocket",
                                          "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60}}))
            hello = json.loads((await ws.receive()).data); sid = hello["session_id"]
            for lang, text in utterances:
                a, sr = tts.synth(text, lang); pcm = (np.clip(to16k(a, sr), -1, 1) * 32767).astype(np.int16)
                pcm = np.concatenate([np.zeros(4800, np.int16), pcm, np.zeros(16000, np.int16)])  # lead-in + 1 s trailing silence
                enc = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
                await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "start", "mode": mode}))
                for i in range(0, len(pcm) - 960 + 1, 960):
                    await ws.send_bytes(enc.encode(pcm[i:i + 960].tobytes(), 960)); await asyncio.sleep(0.06)
                t_end = time.time()
                if mode == "manual":
                    await ws.send_str(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))
                dec = opuslib.Decoder(24000, 1); got = []; events = []; first = None; aborted = None
                while True:
                    m = await asyncio.wait_for(ws.receive(), 120)
                    if m.type == aiohttp.WSMsgType.BINARY:
                        first = first or time.time() - t_end
                        got.append(np.frombuffer(dec.decode(m.data, 1440), np.int16).astype(np.float32) / 32768)
                        if os.environ.get("EMU_ABORT_AFTER") and aborted is None and len(got) * 0.06 > float(os.environ["EMU_ABORT_AFTER"]):
                            aborted = time.time(); await ws.send_str(json.dumps({"session_id": sid, "type": "abort", "reason": "button"}))
                            events.append((round(aborted - t_end, 2), "SENT abort", None, None))
                    elif m.type == aiohttp.WSMsgType.TEXT:
                        o = json.loads(m.data); events.append((round(time.time() - t_end, 2), o.get("type"), o.get("state") or o.get("emotion"), o.get("text")))
                        if o.get("type") == "tts" and o.get("state") == "stop": break
                    else: break
                audio = np.concatenate(got) if got else np.zeros(0)
                heard = stt.transcribe(to16k(audio, 24000))[0] if len(audio) else ""
                print(f"\n>>> [{lang}] {text}")
                for e in events: print("   ", e)
                if aborted: print(f"    frames received after abort: {sum(1 for _ in got) - int(float(os.environ['EMU_ABORT_AFTER'])/0.06) - 1} (expect ~0)")
                print(f"    first audio after end-of-speech: {first and round(first, 2)}s, audio {len(audio)/24000:.1f}s, re-transcribed: {heard}")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "manual"
    sets = {"basic": [("en", "Hi there, what animal says moo?"), ("zh", "你好，你喜欢画画吗？")],
            "yue": [("yue", "你好，你鍾唔鍾意食雪糕呀？")],
            "long": [("en", "Can you tell me a short story about a brave little rabbit?")]}
    asyncio.run(main(sets[sys.argv[2] if len(sys.argv) > 2 else "basic"], mode))
