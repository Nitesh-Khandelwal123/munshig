"""Run the Node.js WhatsApp bridge and exchange JSON lines with it.

FakeChannel stands in for WhatsApp in tests and the offline demo.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class Channel(Protocol):
    def send(self, jid: str, text: str, media_path: str | None = None,
             mime_type: str | None = None) -> str | None: ...

    def typing(self, jid: str, on: bool) -> None: ...

    def events(self) -> "queue.Queue[dict]": ...


class BridgeError(Exception):
    pass


def bridge_dir() -> Path:
    if env := os.environ.get("MUNSHIG_BRIDGE_DIR"):
        return Path(env)
    return Path(__file__).resolve().parents[1] / "bridge"


class WhatsAppBridge:
    def __init__(self, auth_dir: Path, media_dir: Path, node: str | None = None,
                 directory: Path | None = None, send_timeout: float = 60.0):
        self.directory = directory or bridge_dir()
        self.node = node or shutil.which("node")
        if not self.node:
            raise BridgeError("Node.js isn't installed. MunshiG needs Node.js 20 or newer for WhatsApp.")
        if not (self.directory / "node_modules").exists():
            raise BridgeError(f"WhatsApp bridge isn't set up. Run 'npm ci' in {self.directory}.")
        self.auth_dir, self.media_dir = auth_dir, media_dir
        self.send_timeout = send_timeout
        self._events: queue.Queue[dict] = queue.Queue()
        self._waiting: dict[str, queue.Queue] = {}
        self._ids = itertools.count(1)
        self._proc: subprocess.Popen | None = None
        self._write_lock = threading.Lock()

    def start(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self._proc = subprocess.Popen(
            [self.node, str(self.directory / "bridge.mjs"), "--auth-dir", str(self.auth_dir),
             "--media-dir", str(self.media_dir)],
            cwd=self.directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None,  # the bridge draws the login QR code on stderr: show it on the console
            text=True, encoding="utf-8", bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") in ("sent", "send_failed") and event.get("requestId") in self._waiting:
                self._waiting[event["requestId"]].put(event)
            else:
                self._events.put(event)
        code = self._proc.wait()
        self._events.put({"type": "bridge_exit", "code": code})

    def _write(self, cmd: dict) -> None:
        if not self._proc or self._proc.poll() is not None or not self._proc.stdin:
            raise BridgeError("WhatsApp bridge isn't running")
        with self._write_lock:
            self._proc.stdin.write(json.dumps(cmd) + "\n")
            self._proc.stdin.flush()

    def send(self, jid: str, text: str, media_path: str | None = None, mime_type: str | None = None) -> str | None:
        request_id = str(next(self._ids))
        box: queue.Queue = queue.Queue()
        self._waiting[request_id] = box
        cmd = {"type": "send", "requestId": request_id, "jid": jid, "text": text}
        if media_path:
            cmd.update({"mediaPath": media_path, "mimeType": mime_type, "fileName": Path(media_path).name})
        try:
            self._write(cmd)
            reply = box.get(timeout=self.send_timeout)
        except queue.Empty as exc:
            raise BridgeError("WhatsApp didn't confirm the message in time") from exc
        finally:
            self._waiting.pop(request_id, None)
        if reply["type"] == "send_failed":
            raise BridgeError(reply.get("error") or "send failed")
        return reply.get("id")

    def typing(self, jid: str, on: bool) -> None:
        try:
            self._write({"type": "typing", "jid": jid, "on": on})
        except BridgeError:
            pass

    def events(self) -> "queue.Queue[dict]":
        return self._events

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


@dataclass
class FakeChannel:
    """Records what would be sent. Feed incoming events with push()."""
    sent: list[dict] = field(default_factory=list)
    _events: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def send(self, jid: str, text: str, media_path: str | None = None, mime_type: str | None = None) -> str:
        msg_id = f"OUT{next(self._ids)}"
        self.sent.append({"id": msg_id, "jid": jid, "text": text, "media_path": media_path})
        return msg_id

    def typing(self, jid: str, on: bool) -> None:
        pass

    def events(self) -> "queue.Queue[dict]":
        return self._events

    def push(self, event: dict) -> None:
        self._events.put(event)
