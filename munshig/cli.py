"""The `munshig` command.

    munshig init           create the settings and knowledge files
    munshig check          check Tally, the local AI, Node.js and the settings
    munshig tally-sync     read ledgers from Tally (run after adding ledgers)
    munshig link           show the WhatsApp QR code and link the business phone
    munshig run            start MunshiG (WhatsApp + digest + Tally posting)
    munshig digest         print today's digest without sending it
    munshig mock-tally     run a pretend Tally on port 9000 for trying things out
    munshig demo           one dummy business day, end to end, no setup needed
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time

from . import __version__
from .config import default_home, load, save
from .db import DB


def _settings(args):
    from pathlib import Path
    return load(Path(args.home) if args.home else None)


KNOWLEDGE_TEMPLATE = """# What the assistant may tell customers

Write facts here in plain words. The assistant answers ONLY from this file;
anything not written here becomes a follow-up for you instead of a guess.
Prices, timings and policies must be exact.

## About us
(What you sell, where you are.)

## Timings
(e.g. Open Monday to Saturday, 10 am to 8 pm. Closed on Sundays.)

## Prices
(e.g. Product A: Rs 450 per piece, GST extra.)

## Payments
(UPI ID, bank details customers may pay to.)

## Delivery and returns
(What you promise, exactly.)
"""


def cmd_init(args) -> int:
    settings = _settings(args)
    if settings.config_path.exists() and not args.force:
        print(f"Settings already exist at {settings.config_path} (use --force to overwrite).")
    else:
        if args.business:
            settings.business.name = args.business
        if args.gstin:
            settings.business.gstin = args.gstin.upper()
        if args.owner:
            settings.owner.numbers = [args.owner]
        if args.company:
            settings.tally.company = args.company
        settings.tally.educational_mode = args.edu
        path = save(settings)
        print(f"Wrote {path}")
    if not settings.knowledge_path.exists():
        settings.knowledge_path.write_text(KNOWLEDGE_TEMPLATE, encoding="utf-8")
        print(f"Wrote {settings.knowledge_path}: fill it in before switching on replies.")
    print("\nNext: open TallyPrime with your company loaded, then run 'munshig check'.")
    return 0


def cmd_check(args) -> int:
    from .gst import is_valid_gstin
    from .llm import LLMError, OllamaLLM
    from .tally.client import TallyClient, TallyUnavailable
    from .whatsapp import bridge_dir

    s = _settings(args)
    problems = 0

    def say(ok: bool, text: str) -> None:
        nonlocal problems
        problems += 0 if ok else 1
        print(("  OK    " if ok else "  FIX   ") + text)

    print(f"MunshiG {__version__}, data folder {s.home}")
    say(s.config_path.exists(), f"settings file {s.config_path}")
    say(bool(s.business.name), "business name set")
    say(not s.business.gstin or is_valid_gstin(s.business.gstin),
        f"business GSTIN {s.business.gstin or '(none: name matching only)'}")
    say(bool(s.owner.numbers), "owner number set (only it can approve entries)")
    kb = s.knowledge_path.read_text(encoding="utf-8") if s.knowledge_path.exists() else ""
    say(bool(kb) and kb.strip() != KNOWLEDGE_TEMPLATE.strip(), "knowledge file filled in")

    tally = TallyClient(s.tally.host, s.tally.port, s.tally.company, timeout=s.tally.timeout_seconds)
    try:
        companies = tally.companies()
        say(True, f"Tally reachable at {s.tally.host}:{s.tally.port}; open companies: {', '.join(companies) or 'none'}")
        say(s.tally.company in companies, f"company '{s.tally.company}' is open in Tally")
        if s.tally.company in companies:
            names = {ld.name.lower() for ld in tally.ledgers()}
            lg = s.ledgers
            for label, name in (("sales", lg.sales), ("purchase", lg.purchase), ("cash", lg.cash), ("bank", lg.bank),
                                ("output CGST", lg.output_cgst), ("output SGST", lg.output_sgst),
                                ("output IGST", lg.output_igst), ("input CGST", lg.input_cgst),
                                ("input SGST", lg.input_sgst), ("input IGST", lg.input_igst),
                                ("round-off", lg.round_off), ("default expense", lg.default_expense)):
                say(name.lower() in names, f"{label} ledger '{name}' exists in Tally")
    except TallyUnavailable as exc:
        say(False, str(exc))
    if s.tally.educational_mode:
        print("  NOTE  Tally educational mode: vouchers can only be dated the 1st, 2nd or 31st.")

    if s.ai.backend != "none":
        try:
            models = OllamaLLM(url=s.ai.ollama_url).available_models()
            say(True, f"Ollama reachable at {s.ai.ollama_url}")
            for m in (s.ai.chat_model, s.ai.vision_model):
                say(any(x == m or x.startswith(m + ":") for x in models), f"model {m} downloaded (ollama pull {m})")
        except (LLMError, Exception) as exc:  # noqa: BLE001
            say(False, f"Ollama not reachable at {s.ai.ollama_url} ({exc.__class__.__name__}). Install OpenJarvis or Ollama.")

    node = shutil.which("node")
    say(bool(node), f"Node.js installed ({node or 'not found'})")
    say((bridge_dir() / "node_modules").exists(), f"WhatsApp bridge set up in {bridge_dir()} (npm ci)")
    print("\nAll good." if not problems else f"\n{problems} thing(s) to fix.")
    return 0 if not problems else 1


def cmd_tally_sync(args) -> int:
    from .parties import refresh_ledger_cache
    from .tally.client import TallyClient, TallyUnavailable

    s = _settings(args)
    tally = TallyClient(s.tally.host, s.tally.port, s.tally.company, timeout=s.tally.timeout_seconds)
    try:
        n = refresh_ledger_cache(DB(s.db_path), tally.ledgers())
    except TallyUnavailable as exc:
        print(exc)
        return 1
    print(f"Read {n} ledgers from '{s.tally.company}'.")
    return 0


def _bridge(s):
    from .whatsapp import WhatsAppBridge
    return WhatsAppBridge(s.whatsapp_auth_dir, s.media_dir)


def cmd_link(args) -> int:
    s = _settings(args)
    bridge = _bridge(s)
    bridge.start()
    print("Waiting for WhatsApp. When the QR code appears, open WhatsApp on the business phone >"
          " Linked devices > Link a device, and scan it.")
    while True:
        ev = bridge.events().get()
        if ev["type"] == "connected":
            print(f"Linked to {ev.get('me')}. You can now run 'munshig run'.")
            time.sleep(3)  # let the first sync settle
            bridge.stop()
            return 0
        if ev["type"] in ("bridge_exit",) or (ev["type"] == "disconnected" and ev.get("loggedOut")):
            print("Linking failed or was cancelled. Run 'munshig link' again.")
            return 1


def cmd_run(args) -> int:
    from .llm import from_settings
    from .parties import refresh_ledger_cache
    from .service import Service
    from .tally.client import TallyClient, TallyUnavailable

    s = _settings(args)
    if not s.owner.numbers:
        print("Set the owner number first: munshig init --owner 91XXXXXXXXXX")
        return 1
    db = DB(s.db_path)
    tally = TallyClient(s.tally.host, s.tally.port, s.tally.company, timeout=s.tally.timeout_seconds)
    try:
        refresh_ledger_cache(db, tally.ledgers())
    except TallyUnavailable as exc:
        print(f"Warning: {exc}\nMunshiG will keep approvals queued until Tally is reachable.")
    bridge = _bridge(s)
    bridge.start()
    svc = Service(s, db, bridge, tally, from_settings(s.ai))
    print(f"MunshiG is running in {s.assistant.mode} mode. Digest at {s.assistant.digest_time}. Ctrl+C to stop.")
    try:
        svc.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


def cmd_digest(args) -> int:
    from .digest import build_digest
    s = _settings(args)
    text, _ = build_digest(DB(s.db_path))
    print(text)
    return 0


def cmd_mock_tally(args) -> int:
    from .tally.mock import MockTally
    mock = MockTally(company=args.company, educational_mode=args.edu)
    mock.serve(port=args.port)
    print(f"Pretend Tally on port {args.port}, company '{args.company}'. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{len(mock.vouchers)} vouchers were posted.")
    return 0


def cmd_demo(args) -> int:
    from .demo import run_demo
    return run_demo(tally_addr=args.tally, company=args.company, edu=not args.no_edu, ai=args.ai, keep=args.keep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="munshig", description="MunshiG: WhatsApp to Tally, on your own PC.")
    parser.add_argument("--home", help=f"data folder (default {default_home()})")
    parser.add_argument("--version", action="version", version=f"munshig {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the settings and knowledge files")
    p.add_argument("--business")
    p.add_argument("--gstin")
    p.add_argument("--owner", help="owner WhatsApp number with country code, e.g. 919812345678")
    p.add_argument("--company", help="Tally company name, exactly as in Tally")
    p.add_argument("--edu", action="store_true", help="Tally is in free educational mode")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    sub.add_parser("check", help="check that everything is set up").set_defaults(func=cmd_check)
    sub.add_parser("tally-sync", help="read ledgers from Tally").set_defaults(func=cmd_tally_sync)
    sub.add_parser("link", help="link the business WhatsApp by QR code").set_defaults(func=cmd_link)
    sub.add_parser("run", help="start MunshiG").set_defaults(func=cmd_run)
    sub.add_parser("digest", help="print the digest without sending it").set_defaults(func=cmd_digest)

    p = sub.add_parser("mock-tally", help="run a pretend Tally server")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--company", default="Demo Traders")
    p.add_argument("--edu", action="store_true")
    p.set_defaults(func=cmd_mock_tally)

    p = sub.add_parser("demo", help="run one dummy business day end to end")
    p.add_argument("--tally", help="HOST:PORT of a real TallyPrime (use a test company)")
    p.add_argument("--company", default="Demo Traders")
    p.add_argument("--no-edu", action="store_true", help="the Tally is licensed (not educational mode)")
    p.add_argument("--ai", choices=["scripted", "real"], default="scripted")
    p.add_argument("--keep", action="store_true", help="keep the demo's data folder")
    p.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # ₹ and Hindi text on any Windows console
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
