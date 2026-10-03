// StackChan channel plugin for OpenClaw.
// Fixed route: the one authenticated voice edge -> one configured agent, in a dedicated daily session. The request
// can carry only a turn id, a transcript and a language; it can never pick agent, session, model or tools.
// Persists status/hashes only — the conversation text lives in the agent's normal transcript.
import { definePluginEntry } from 'openclaw/plugin-sdk/plugin-entry';
import https from 'node:https';
import fs from 'node:fs';
import crypto from 'node:crypto';

const MAX_BODY = 16 * 1024;
const TURN_FIELDS = new Set(['turn_id', 'transcript', 'language', 'interaction']);
const LANGS = new Set(['en', 'zh', 'yue']);
const MOTIONS = ['look_left', 'look_right', 'look_up', 'look_down', 'look_center', 'nod', 'shake_head', 'volume_up', 'volume_down'];
const MOVE_SCHEMA = { type: 'object', additionalProperties: false, required: ['action'],
  properties: { action: { type: 'string', enum: MOTIONS, description: 'Head movement or speaker volume change for the StackChan robot' } } };

function settings(cfg) {
  const child = cfg.childName || 'the child';
  return {
    agent: cfg.agentId || 'main',
    account: cfg.accountId || 'stackchan',
    edgeCN: cfg.edgeCertCN || 'stackchan-edge',
    tz: cfg.timeZone || 'UTC',
    child,
    agentName: cfg.agentName || null,
    logSuffix: cfg.logFileSuffix || cfg.accountId || 'stackchan',
    host: cfg.listenHost || '0.0.0.0',
    port: cfg.port || 7444,
    prompt: [
      `You are speaking OUT LOUD to ${child} through a StackChan desk robot. Everything you write is converted to speech.`,
      'Keep it short and conversational: one to three sentences unless asked for a story or more detail.',
      'Never use markdown, lists, emoji, URLs, file paths or code — speak in plain sentences.',
      `The robot authenticates the device, not the speaker: treat the speaker as ${child} but apply all your normal rules and approvals unchanged.`,
      'This voice conversation is separate from your other channels; do not mention device internals.',
      'Words reach you through local speech recognition, which can mishear names and cut words short' +
        (cfg.nameMishearings?.length ? ` (your name may arrive as ${cfg.nameMishearings.join(', ')})` : '') +
        '. Read generously for what was most likely said; never correct the speaker for recognition errors.',
      'You have a body: the stackchan_move tool turns the robot head (look_left, look_right, look_up, look_down, look_center, nod, shake_head) and changes your speaker volume (volume_up, volume_down). Use it when asked to move or look somewhere, or occasionally to be expressive; it runs while you speak, so still answer in words.',
      cfg.conversationLogDir
        ? `Speed matters: ${child} is waiting to hear you. The StackChan channel appends this exchange to your memory/conversations/<date>-${cfg.logFileSuffix || cfg.accountId || 'stackchan'}.md log for you after you answer, so do NOT read or edit that log during the turn. Only use a tool when the answer genuinely needs one.`
        : `Speed matters: ${child} is waiting to hear you. Only use a tool when the answer genuinely needs one.`,
    ].join(' '),
  };
}

const dateIn = (tz, ms) => new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).format(ms);

function messageText(m) {
  if (typeof m?.content === 'string') return m.content;
  if (Array.isArray(m?.content)) return m.content.filter((p) => p?.type === 'text' && typeof p.text === 'string').map((p) => p.text).join('\n');
  return typeof m?.text === 'string' ? m.text : '';
}

// Speakable answer only: drop NO_REPLY/control tokens, a trailing channel signature, markdown remnants.
function speakable(text, agentName) {
  if (!text) return '';
  let t = text.trim();
  if (/^NO_REPLY\b/.test(t) || /^HEARTBEAT_OK\b/.test(t)) return '';
  if (agentName) t = t.replace(new RegExp(`\\s*[—–-]{1,2}\\s*${agentName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*·[^\\n]*$`, 'u'), '');
  t = t.replace(/```[\s\S]*?```/g, ' ').replace(/`([^`]*)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/https?:\/\/\S+/g, '')
    .replace(/^[#>*\-\s]+/gm, '').replace(/[*_~]+/g, '')
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]/gu, '')
    .replace(/\s+\n/g, '\n').trim();
  return t.slice(0, 4000);
}

class StackChan {
  constructor(api) {
    this.api = api;
    this.cfg = api.pluginConfig || {};
    this.s = settings(this.cfg);
    this.turns = new Map(); // turn_id -> {promise, cancelled, at}
    // sessionKey -> motion symbols the agent requested during the active turn. Process-wide, because the gateway
    // calls register() more than once: the tool can run in a different instance than the HTTP handler.
    this.motions = (globalThis.__stackchanMotionQueues ||= new Map());
  }

  sessionKeyFor(date) { return `agent:${this.s.agent}:stackchan:${this.s.account}:${date}`; }

  log(rec) {
    if (!this.cfg.statusLogPath) return;
    try { fs.appendFileSync(this.cfg.statusLogPath, JSON.stringify({ at: new Date().toISOString(), ...rec }) + '\n', { mode: 0o600 }); }
    catch (e) { this.api.logger?.warn?.(`stackchan: status log write failed: ${e.message}`); }
  }

  // Optional: append the exchange to the agent's own daily conversation log ("[HH:MM] Name: ...").
  appendConversationLog(ms, said, answer) {
    if (!this.cfg.conversationLogDir) return;
    const hm = new Intl.DateTimeFormat('en-GB', { timeZone: this.s.tz, hour: '2-digit', minute: '2-digit', hour12: false }).format(ms);
    const flat = (x) => x.replace(/\s+/g, ' ').trim();
    const file = `${this.cfg.conversationLogDir}/${dateIn(this.s.tz, ms)}-${this.s.logSuffix}.md`;
    const who = this.cfg.childName || 'Child';
    try { fs.appendFileSync(file, `[${hm}] ${who} (StackChan voice): ${flat(said)}\n[${hm}] ${this.s.agentName || 'Agent'}: ${flat(answer)}\n`); }
    catch (e) { this.api.logger?.warn?.(`stackchan: conversation log append failed: ${e.message}`); }
  }

  async capsule(today) {
    // Continuity: compact recent turns from yesterday's StackChan session only.
    const y = dateIn(this.s.tz, Date.parse(today + 'T12:00:00Z') - 86400000);
    try {
      const res = await this.api.runtime.subagent.getSessionMessages({ sessionKey: this.sessionKeyFor(y), limit: 12 });
      const msgs = (Array.isArray(res) ? res : res?.messages || []).filter((m) => m?.role === 'user' || m?.role === 'assistant');
      const lines = msgs.map((m) => `${m.role === 'user' ? (this.cfg.childName || 'Child') : 'You'}: ${messageText(m).replace(/\s+/g, ' ').slice(0, 240)}`).filter((l) => l.length > 6);
      if (!lines.length) return '';
      return `Recent StackChan conversation from yesterday (${y}), for continuity only:\n` + lines.join('\n').slice(-2000);
    } catch { return ''; }
  }

  async runTurn(body) {
    const t0 = Date.now();
    const today = dateIn(this.s.tz, t0);
    // 'selftest' turns (from a localhost test edge) use a separate session, so tests never enter the
    // child's conversation or continuity capsule.
    const selftest = body.interaction === 'selftest';
    const sessionKey = selftest ? `agent:${this.s.agent}:stackchan:stackchan-selftest:${today}` : this.sessionKeyFor(today);
    const prompt = [this.s.prompt];
    const spoken = { en: 'English', zh: 'Mandarin Chinese', yue: 'Cantonese' }[body.language];
    if (spoken) prompt.push(`${this.s.child} just spoke ${spoken}; reply in ${spoken}.`);
    if (selftest) prompt.push('AUTOMATED SYSTEM TEST, not a real user. Answer normally, but do not write to any memory, log or other file.');
    const cap = selftest ? '' : await this.capsule(today);
    if (cap) prompt.push(cap);
    this.motions.set(sessionKey, []);
    const run = await this.api.runtime.subagent.run({
      sessionKey, message: body.transcript, extraSystemPrompt: prompt.join('\n\n'),
      deliver: false, idempotencyKey: `stackchan:${body.turn_id}`,
      ...(this.cfg.model ? { model: this.cfg.model } : {}),
    });
    const waited = await this.api.runtime.subagent.waitForRun({ runId: run.runId, timeoutMs: 90_000 });
    if (!['ok', 'completed', 'success'].includes(waited?.status)) return { status: 'failed', code: `run-${waited?.status || 'unknown'}`, ms: Date.now() - t0 };
    const res = await this.api.runtime.subagent.getSessionMessages({ sessionKey, limit: 4 });
    const msgs = Array.isArray(res) ? res : res?.messages || [];
    let text = '';
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i]?.role === 'assistant') { text = messageText(msgs[i]); if (text.trim()) break; }
    const answer = speakable(text, this.s.agentName);
    const motions = (this.motions.get(sessionKey) || []).slice(0, 4);
    this.motions.delete(sessionKey);
    // A turn the edge cancelled (e.g. routed straight to the motors) was never spoken: keep it out of the log.
    if (!selftest && answer && !this.turns.get(body.turn_id)?.cancelled) this.appendConversationLog(t0, body.transcript, answer);
    return { status: 'completed', text: answer, motions, session_date: today, run_id: run.runId, ms: Date.now() - t0 };
  }

  async handle(req, res) {
    const send = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); };
    const peer = req.socket.getPeerCertificate?.();
    if (!req.client.authorized || peer?.subject?.CN !== this.s.edgeCN) return send(403, { status: 'rejected', code: 'peer' });
    if (req.method !== 'POST') return send(405, { status: 'rejected' });
    let raw = '';
    for await (const chunk of req) { raw += chunk; if (raw.length > MAX_BODY) return send(413, { status: 'rejected', code: 'size' }); }
    let body;
    try { body = JSON.parse(raw); } catch { return send(400, { status: 'rejected', code: 'json' }); }
    if (!body || typeof body !== 'object' || Object.keys(body).some((k) => !TURN_FIELDS.has(k)) ||
        typeof body.turn_id !== 'string' || !/^[0-9a-f-]{8,64}$/.test(body.turn_id)) return send(400, { status: 'rejected', code: 'envelope' });

    if (req.url === '/v1/interrupt') {
      const t = this.turns.get(body.turn_id);
      if (t) t.cancelled = true;
      this.log({ ev: 'interrupt', turn_id: body.turn_id, known: !!t });
      return send(200, { status: 'cancelled' });
    }
    if (req.url !== '/v1/utterance') return send(404, { status: 'rejected' });
    const transcript = typeof body.transcript === 'string' ? body.transcript.replace(/[\u0000-\u001f\u007f]/g, ' ').trim() : '';
    if (body.interaction !== undefined && !['ptt', 'wake', 'selftest'].includes(body.interaction)) return send(400, { status: 'rejected', code: 'interaction' });
    if (!transcript || [...transcript].length > 2000 || (body.language && !LANGS.has(body.language))) return send(400, { status: 'rejected', code: 'transcript' });

    let t = this.turns.get(body.turn_id);
    const duplicate = !!t;
    if (!t) {
      t = { cancelled: false, at: Date.now() };
      t.promise = this.runTurn({ ...body, transcript }).catch((e) => ({ status: 'failed', code: 'exception', detail: String(e?.message || e).slice(0, 200) }));
      this.turns.set(body.turn_id, t);
      for (const [k, v] of this.turns) if (Date.now() - v.at > 48 * 3600e3) this.turns.delete(k);
    }
    const out = await t.promise;
    const result = t.cancelled ? { status: 'cancelled' } : { ...out, duplicate };
    this.log({ ev: 'turn', turn_id: body.turn_id, status: result.status, code: result.code, ms: out.ms, duplicate,
      lang: body.language, in_sha: crypto.createHash('sha256').update(transcript).digest('hex').slice(0, 16),
      out_sha: out.text ? crypto.createHash('sha256').update(out.text).digest('hex').slice(0, 16) : null });
    return send(200, result);
  }

  // The agent's body: available only inside its StackChan voice sessions; it queues a bounded symbol that
  // the voice edge turns into motor commands within the robot's safe envelope.
  moveTool(context) {
    if (typeof context?.sessionKey !== 'string' || !context.sessionKey.startsWith(`agent:${this.s.agent}:stackchan:`)) return null;
    return {
      name: 'stackchan_move', description: "Move the StackChan robot's head or change its speaker volume while you speak.", parameters: MOVE_SCHEMA,
      execute: async (_id, raw) => {
        const action = raw && typeof raw === 'object' ? raw.action : undefined;
        const q = this.motions.get(context.sessionKey);
        const ok = MOTIONS.includes(action) && !!q && q.length < 4;
        if (ok) q.push(action);
        const value = ok ? { ok: true, note: 'The robot will do this as you speak.' } : { ok: false, code: 'not-available' };
        return { content: [{ type: 'text', text: JSON.stringify(value) }], details: value };
      },
    };
  }

  register() {
    const api = this.api;
    api.registerTool((context) => this.moveTool(context), { name: 'stackchan_move' });
    let server;
    api.registerService({
      id: 'stackchan-channel',
      start: async () => {
        const c = this.cfg;
        server = https.createServer({
          key: fs.readFileSync(c.tlsKeyPath), cert: fs.readFileSync(c.tlsCertPath), ca: fs.readFileSync(c.tlsCaPath),
          requestCert: true, rejectUnauthorized: true, minVersion: 'TLSv1.3',
        }, (req, res) => { this.handle(req, res).catch((e) => { try { res.writeHead(500); res.end('{"status":"failed"}'); } catch {} api.logger?.warn?.(`stackchan: ${e.message}`); }); });
        server.requestTimeout = 120_000;
        await new Promise((ok, bad) => { server.once('error', bad); server.listen(this.s.port, this.s.host, ok); });
        api.logger?.info?.(`stackchan: channel listening on ${this.s.host}:${this.s.port} (mTLS, agent=${this.s.agent})`);
      },
      stop: async () => { if (server) await new Promise((ok) => server.close(ok)); },
    });
  }
}

export default definePluginEntry({
  id: 'stackchan', name: 'StackChan', description: 'StackChan voice terminal channel (fixed route to one agent)',
  register(api) { const s = new StackChan(api); s.register(); return s; },
});
