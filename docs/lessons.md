# Lessons from the first build

Traps hit while bringing this up on a real robot, kept here so you don't repeat them.

- **Opening the robot's USB serial port resets it.** The CoreS3 native USB-Serial/JTAG resets the chip on open
  (`rst:0x15`), so a serial monitor kicks the robot out of the AI Agent app. Use the edge logs instead.
- **The stock firmware boots to a launcher.** Wi-Fi and the voice service start only inside **AI Agent**, so you
  need a tap after every reboot.
- **Long flash reads drop on native USB.** Read in 1 MB chunks with retries (`tools/backup_flash.sh`). Hold the
  device in bootloader (`--after no-reset`) if it must stay silent.
- **The volume is clamped to at least 10 at boot.** "Silent" testing therefore means keeping the device in
  bootloader, not volume 0.
- **SenseVoice's language tag was unreliable for us**: it tagged English as Cantonese. The edge detects
  language from the transcript text instead.
- **Hands-free (auto) listening never stops by itself.** If the server ignores a fragment it must keep listening;
  otherwise the device streams into the void and stops hearing its wake word. The edge re-arms, and it closes
  the session after 30 s of silence.
- **Agent speed is the whole latency budget.**
  - A large agent prompt on a slow model took 7–11 s.
  - A fast model for the voice session cut that to about 1.5–3 s.
  - Telling the agent not to do its own file logging mid-turn saved two model round-trips; the plugin writes
    the log instead.
- **OpenClaw plugin gotchas** (2026.9.5):
  - The manifest needs `"activation": {"onStartup": true}`, or the plugin is never loaded.
  - The gateway calls `register()` more than once, so state shared between a tool and a service must be
    process-wide.
  - A plugin model override needs the model in `agents.defaults.models`, in `agents.defaults.modelPolicy.allow`,
    and in `plugins.entries.<id>.subagent.allowModelOverride` / `allowedModels`.
  - Restart the gateway cleanly after editing plugin entries.
