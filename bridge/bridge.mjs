// MunshiG WhatsApp bridge.
//
// Links to the business's WhatsApp as a linked device (like WhatsApp Web) using
// Baileys, and speaks JSON lines with the MunshiG Python service:
//
//   stdout (events, one JSON object per line):
//     {"type":"qr","qr":"..."}                     scan this to link the phone
//     {"type":"connected","me":"...","meLid":"..."}
//     {"type":"disconnected","loggedOut":bool,"code":n}
//     {"type":"message", id, jid, jidAlt, participant, fromMe, sentByBridge, isGroup,
//        isBroadcast, pushName, timestamp, kind, text, mediaPath, mimeType, fileName,
//        sha256, viewOnce}
//     {"type":"edit", id, jid, text}               a customer edited a message
//     {"type":"revoke", id, jid}                   a customer deleted a message
//     {"type":"sent","requestId","id"} / {"type":"send_failed","requestId","error"}
//     {"type":"log","level","message"}
//
//   stdin (commands):
//     {"type":"send","requestId","jid","text"}
//     {"type":"send","requestId","jid","text","mediaPath","mimeType","fileName"}
//     {"type":"typing","jid","on":true|false}
//     {"type":"logout"}
//
// Unofficial: WhatsApp does not allow automated clients and can restrict the
// number. MunshiG only replies to people who wrote first, at human pace.
//
// Usage: node bridge.mjs --auth-dir <folder> --media-dir <folder>

import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync, readFileSync } from "node:fs";
import { basename, extname, join } from "node:path";
import * as readline from "node:readline";

import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  generateMessageIDV2,
  getContentType,
  isJidBroadcast,
  isJidGroup,
  isJidStatusBroadcast,
  normalizeMessageContent,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import { HttpsProxyAgent } from "https-proxy-agent";
import pino from "pino";
import qrcodeTerminal from "qrcode-terminal";

const emit = (event) => process.stdout.write(JSON.stringify(event) + "\n");
const log = (level, message) => emit({ type: "log", level, message });

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const authDir = arg("--auth-dir", "./whatsapp-auth");
const mediaDir = arg("--media-dir", "./media");
mkdirSync(authDir, { recursive: true });
mkdirSync(mediaDir, { recursive: true });

// Baileys logs go to stderr so stdout stays clean JSON.
const logger = pino({ level: process.env.MUNSHIG_BRIDGE_LOG || "warn" }, pino.destination(2));

const EXT = {
  "application/pdf": ".pdf",
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "audio/ogg; codecs=opus": ".ogg",
  "audio/ogg": ".ogg",
  "audio/mpeg": ".mp3",
  "audio/mp4": ".m4a",
  "video/mp4": ".mp4",
};

const sentByUs = new Set(); // message ids this bridge sent, to tell them from staff typing on the phone
let sock = null;
let retry = 0;

function rememberSent(id) {
  sentByUs.add(id);
  if (sentByUs.size > 5000) sentByUs.delete(sentByUs.values().next().value);
}

function kindOf(type) {
  switch (type) {
    case "conversation":
    case "extendedTextMessage":
      return "text";
    case "imageMessage":
      return "image";
    case "documentMessage":
      return "document";
    case "audioMessage":
      return "audio";
    case "videoMessage":
      return "video";
    case "stickerMessage":
      return "sticker";
    case "locationMessage":
    case "liveLocationMessage":
      return "location";
    case "contactMessage":
    case "contactsArrayMessage":
      return "contact";
    default:
      return "other";
  }
}

async function handleMessage(msg) {
  const key = msg.key || {};
  const jid = key.remoteJid || "";
  if (!msg.message || !jid) return;
  const content = normalizeMessageContent(msg.message);
  const type = getContentType(content);
  if (!type) return;

  if (type === "protocolMessage") {
    const p = content.protocolMessage;
    if (p?.type === 0 /* REVOKE */) emit({ type: "revoke", id: p.key?.id, jid });
    else if (p?.editedMessage) {
      const edited = normalizeMessageContent(p.editedMessage);
      const text = edited?.conversation || edited?.extendedTextMessage?.text || "";
      emit({ type: "edit", id: p.key?.id, jid, text });
    }
    return;
  }
  if (type === "reactionMessage" || type === "senderKeyDistributionMessage") return;

  const body = content[type] || {};
  const kind = kindOf(type);
  const event = {
    type: "message",
    id: key.id,
    jid,
    jidAlt: key.remoteJidAlt || null,
    participant: key.participant || null,
    fromMe: !!key.fromMe,
    sentByBridge: !!key.fromMe && sentByUs.has(key.id),
    isGroup: !!isJidGroup(jid),
    isBroadcast: !!(isJidBroadcast(jid) || isJidStatusBroadcast(jid)),
    pushName: msg.pushName || null,
    timestamp: Number(msg.messageTimestamp || Math.floor(Date.now() / 1000)),
    kind,
    text: content.conversation || body.text || body.caption || "",
    mediaPath: null,
    mimeType: body.mimetype || null,
    fileName: body.fileName || null,
    sha256: null,
    viewOnce: !!(body.viewOnce || key.isViewOnce),
  };

  const wantMedia = ["image", "document", "audio"].includes(kind) && !event.isGroup && !event.isBroadcast;
  if (wantMedia && !event.sentByBridge) {
    try {
      const buffer = await downloadMediaMessage(msg, "buffer", {}, {
        logger,
        reuploadRequest: sock.updateMediaMessage,
      });
      const ext = EXT[event.mimeType] || extname(event.fileName || "") || ".bin";
      const safeId = String(key.id).replace(/[^A-Za-z0-9_-]/g, "");
      const path = join(mediaDir, `${safeId}${ext}`);
      writeFileSync(path, buffer);
      event.mediaPath = path;
      event.sha256 = createHash("sha256").update(buffer).digest("hex");
    } catch (err) {
      log("warn", `Could not download media ${key.id}: ${err.message}`);
    }
  }
  emit(event);
}

async function start() {
  const { state, saveCreds } = await useMultiFileAuthState(authDir);
  // Offices that reach the internet through a proxy set HTTPS_PROXY.
  const proxy = process.env.HTTPS_PROXY || process.env.https_proxy;
  const agent = proxy ? new HttpsProxyAgent(proxy) : undefined;
  sock = makeWASocket({
    auth: state,
    logger,
    agent,
    fetchAgent: agent,
    browser: Browsers.windows("MunshiG"),
    markOnlineOnConnect: false, // keep the phone getting notifications
    syncFullHistory: false,
  });
  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      qrcodeTerminal.generate(qr, { small: true }, (code) => process.stderr.write(code + "\n"));
      emit({ type: "qr", qr });
    }
    if (connection === "open") {
      retry = 0;
      emit({ type: "connected", me: sock.user?.id || null, meLid: sock.user?.lid || null });
    } else if (connection === "close") {
      const code = lastDisconnect?.error?.output?.statusCode ?? 0;
      const loggedOut = code === DisconnectReason.loggedOut;
      emit({ type: "disconnected", loggedOut, code });
      if (loggedOut) process.exit(2);
      retry += 1;
      const wait = Math.min(60000, 2000 * 2 ** Math.min(retry, 5));
      setTimeout(() => start().catch((e) => log("error", `Restart failed: ${e.message}`)), wait);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify" && type !== "append") return;
    for (const msg of messages) {
      try {
        await handleMessage(msg);
      } catch (err) {
        log("error", `Message handling failed: ${err.message}`);
      }
    }
  });
}

async function send(cmd) {
  if (!sock) throw new Error("WhatsApp is not connected");
  const messageId = generateMessageIDV2(sock.user?.id);
  rememberSent(messageId);
  let content;
  if (cmd.mediaPath) {
    const data = readFileSync(cmd.mediaPath);
    const mime = cmd.mimeType || "application/octet-stream";
    content = mime.startsWith("image/")
      ? { image: data, caption: cmd.text || undefined, mimetype: mime }
      : { document: data, mimetype: mime, fileName: cmd.fileName || basename(cmd.mediaPath), caption: cmd.text || undefined };
  } else {
    content = { text: cmd.text };
  }
  const sent = await sock.sendMessage(cmd.jid, content, { messageId });
  return sent?.key?.id || messageId;
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  let cmd;
  try {
    cmd = JSON.parse(line);
  } catch {
    log("error", "Invalid JSON on stdin");
    return;
  }
  try {
    if (cmd.type === "send") {
      const id = await send(cmd);
      emit({ type: "sent", requestId: cmd.requestId, id });
    } else if (cmd.type === "typing" && sock) {
      await sock.sendPresenceUpdate(cmd.on ? "composing" : "paused", cmd.jid);
    } else if (cmd.type === "logout" && sock) {
      await sock.logout();
      process.exit(0);
    }
  } catch (err) {
    if (cmd.type === "send") emit({ type: "send_failed", requestId: cmd.requestId, error: err.message });
    else log("error", `${cmd.type} failed: ${err.message}`);
  }
});
rl.on("close", () => process.exit(0));

start().catch((err) => {
  log("error", `Fatal: ${err.message}`);
  process.exit(1);
});
