# M5Stack StackChan × OpenClaw agent integration

Turn an off-the-shelf **M5Stack StackChan** (CoreS3) into a voice body for an existing
[OpenClaw](https://docs.openclaw.ai) agent, with **speech recognition and speech synthesis running on your own
machine** and **no firmware flashing**.

The robot keeps its stock firmware. A small settings change points it at a local "voice edge". The edge turns
speech into text locally and hands the text to your OpenClaw agent through a dedicated channel plugin. It then
speaks the agent's answer back through the robot and moves the robot's head when the agent asks.

```
StackChan (stock firmware)                 Edge host (macOS/Linux)                       OpenClaw gateway
 wake word "Hi, Stack Chan" ──WS/Opus──▶  edge.py                                         plugin/ (channel "stackchan")
 mic / speaker / servos / LEDs ◀────────   ├─ SenseVoice STT  (sherpa-onnx, local)  ──mTLS──▶ fixed route → one agent
                              ◀──MCP────   ├─ Kokoro / VITS TTS (local)                       dedicated daily session
                                           ├─ device MCP: head, LEDs, volume (allowlist)      stackchan_move tool
                                           └─ optional intent triage (TypeSafe Jev)           agent's normal rules/tools
```

## What you get

- **Talk to your agent** after the stock wake word "Hi, Stack Chan". Follow-up questions need no wake word for
  30 s; after 30 s of silence the session closes and the robot stops streaming audio.
- **Local speech, three languages:** English, Mandarin and Cantonese STT (SenseVoice). Kokoro TTS covers English
  and Mandarin, and a VITS model covers Cantonese. The agent is told which language the child used and replies
  in it.
- **The agent stays the agent:** the request carries only text. It cannot pick the agent, session, model or
  tools, so the agent's own identity, memory, rules and approvals apply unchanged.
- **A body:** the agent gets a `stackchan_move` tool (look left/right/up/down/centre, nod, shake head, volume
  up/down). The edge executes it within a fixed, clamped envelope; the firmware enforces its own limits on top.
- **Fast paths:**
  - A spoken "Let me think" plays if the agent hasn't answered after 1.8 s.
  - Pure body commands ("look to the left", "louder") can go straight to the motors in about 1.3 s. This uses
    an optional intent classifier that runs in parallel with the agent.
- **Child-friendly cues:** LED green = listening, blue = thinking, off = speaking or asleep. Output is processed
  for the small 1 W speaker (high-pass, loudness, soft limiter).
- **Privacy:**
  - Audio lives in memory only; logs carry metadata, never conversation text.
  - The robot's camera, reboot, firmware-upgrade and asset-download tools are never called.
  - The redirect removes the vendor-cloud MQTT credentials from the device.

Measured on a real robot (M4 Pro Mac mini edge, cloud LLM for the voice session): first spoken word **≈1.4–3 s**
after the child stops talking. STT takes about 70 ms and TTS 200–500 ms per sentence.

## Hardware and software tested

- M5Stack StackChan (CoreS3, ESP32-S3, 16 MB flash), stock firmware **v1.5.1** (xiaozhi-based, IDF 5.5.4)
- Edge: macOS 26 on Apple Silicon, Python 3.14 (Linux should work; the launchd plists are macOS-only)
- OpenClaw **2026.9.5**

## Setup

> ⚠️ **Do not open the robot's USB serial port while you are using it.** On the CoreS3 native USB, opening the
> port resets the chip (`rst:0x15 USB_UART_CHIP_RESET`) and drops it out of the AI Agent app.

1. **Back up the robot first.** This makes everything reversible.
   ```
   pip install esptool
   PORT=/dev/cu.usbmodemXXXX tools/backup_flash.sh backups/factory.bin
   ```
   Run it twice and compare the hashes. Keep the image private: it contains your Wi-Fi password.
   Restore at any time with `esptool --port $PORT write-flash 0x0 backups/factory.bin`.
2. **Edge environment:**
   ```
   python3 -m venv edge-venv && edge-venv/bin/pip install -r edge/requirements.txt esp-idf-nvs-partition-gen
   tools/fetch_models.sh
   cp edge/config.example.json edge/config.json   # set bind IP, channel URL, device MAC + client id (see step 4)
   tools/make_pki.sh <openclaw-host-ip> pki        # private CA + mTLS certs for the edge ↔ plugin link
   ```
3. **OpenClaw plugin:**
   - Copy `pki/ca.crt` and `pki/channel.{crt,key}` to the gateway host.
   - Merge `plugin/openclaw-entry.example.json` into `openclaw.json`. Set `agentId`, `childName`, `timeZone`
     and the `model` used for voice turns.
   - Run `openclaw config validate`, then restart the gateway.
   - Run `openclaw channels status`.
4. **Redirect the robot** (no firmware flash):
   ```
   esptool --port $PORT read-flash 0x9000 0x4000 tools/nvs_original.bin
   tools/fetch_nvs_tools.sh
   cd tools && ../edge-venv/bin/python build_nvs.py build <edge-ip>:8003
   ../edge-venv/bin/python -m esp_idf_nvs_partition_gen generate nvs_local.csv nvs_local.bin 0x4000
   ../edge-venv/bin/python build_nvs.py diff        # Wi-Fi, uuid and servo calibration must say "same"
   esptool --port $PORT write-flash 0x9000 nvs_local.bin
   ```
   - The device MAC and client id (board uuid) for `config.json` are printed by `esptool flash-id` and
     `build_nvs.py diff`.
   - If the robot can't find its stored Wi-Fi, it opens a setup hotspot; join it from a phone. The local OTA
     URL survives that.
5. **Run the edge:**
   - Run `edge-venv/bin/python edge/edge.py`, or install the launchd templates in `deploy/`, including the
     5-minute watchdog.
   - On the robot, open **AI Agent** and say "Hi, Stack Chan".
6. **Test silently:**
   - Run a second edge with `EDGE_TEST_INSTANCE=1 EDGE_BIND=127.0.0.1 EDGE_PORT=8013`.
   - Then run `edge/emulator.py manual basic`.
   - The emulator speaks the device protocol with synthesized speech and transcribes the reply. Its turns go to
     a separate self-test session.

Optional intent triage: set `TYPESAFE_API_KEY` (or `jev_key_command` in `config.json`). This sends the
**transcript text** to TypeSafe's API. Leave it unset to keep everything except the agent's own LLM call on
your network.

## Security model and known limits

This is a household pilot, not a hardened product:
- **Stock firmware:** the vendor endpoints remain compiled in. The redirect removes the cloud credentials and
  serves no MQTT config. For a hard guarantee, block the robot's internet egress at your router.
- **Plain WebSocket on the LAN:** device ↔ edge uses `ws://` with a per-device token plus a MAC/client-id
  check, because the stock firmware can't pin a private CA. Edge ↔ OpenClaw is mutual TLS 1.3.
- **Device, not speaker:** the robot authenticates the device, not the person speaking. Your agent's own rules
  and approvals remain the control.
- **Fixed wake word:** it's the firmware's built-in "Hi, Stack Chan" (Espressif WakeNet). A custom wake word
  needs custom firmware (e.g. microWakeWord).
- **Separate timbres:** each language uses its own TTS voice, so there's no single cross-language voice.
- **Remote not supported:** the stock remote (ESP-NOW) drives motion only and can't be used as push-to-talk
  without a firmware change.

## Credits

- [78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — the device protocol and the
  [MCP protocol doc](https://github.com/78/xiaozhi-esp32/blob/main/docs/mcp-protocol.md) this edge implements.
- [m5stack/StackChan](https://github.com/m5stack/StackChan), [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx),
  SenseVoice, Kokoro, Silero VAD, Espressif ESP-IDF NVS tools.
- [rebelthor/warble](https://github.com/rebelthor/warble) for its clear protocol write-up, and
  [Jonaaaa/StackChan-LocalAI](https://github.com/Jonaaaa/StackChan-LocalAI) for the NVS `ota_url` redirect idea.

## License

MIT. See [LICENSE](LICENSE). Model weights and third-party tools keep their own licenses.
