"""Settings, stored as one TOML file the owner can read and edit.

Location: %APPDATA%\\MunshiG on Windows, ~/.munshig elsewhere, or the folder
named by the MUNSHIG_HOME environment variable.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def default_home() -> Path:
    if env := os.environ.get("MUNSHIG_HOME"):
        return Path(env)
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "MunshiG"
    return Path.home() / ".munshig"


@dataclass
class Business:
    name: str = ""
    gstin: str = ""
    phone: str = ""
    languages: list[str] = field(default_factory=lambda: ["English", "Hindi", "Hinglish"])


@dataclass
class Owner:
    # Phone numbers with country code, digits only, e.g. "919812345678".
    # Only these numbers can approve entries or answer follow-ups.
    numbers: list[str] = field(default_factory=list)


@dataclass
class Tally:
    host: str = "127.0.0.1"
    port: int = 9000
    company: str = ""
    # The free (educational) mode of TallyPrime only accepts vouchers dated
    # the 1st, 2nd or 31st of a month.
    educational_mode: bool = False
    # "nearest_allowed": move the date to the nearest allowed day and keep the
    # real date in the narration. "refuse": do not post, tell the owner.
    edu_date_policy: str = "nearest_allowed"
    timeout_seconds: float = 20.0


@dataclass
class Ledgers:
    sales: str = "Sales"
    purchase: str = "Purchase"
    output_cgst: str = "Output CGST"
    output_sgst: str = "Output SGST"
    output_igst: str = "Output IGST"
    input_cgst: str = "Input CGST"
    input_sgst: str = "Input SGST"
    input_igst: str = "Input IGST"
    round_off: str = "Round Off"
    cash: str = "Cash"
    bank: str = "Bank"
    default_expense: str = "General Expenses"
    default_asset: str = ""
    sundry_debtors: str = "Sundry Debtors"
    sundry_creditors: str = "Sundry Creditors"
    # Where money for a bill-less expense or asset came from: "cash" or "bank".
    paid_from: str = "cash"


@dataclass
class AI:
    # "ollama" talks to a local Ollama server directly. "openjarvis" routes
    # through an installed OpenJarvis. "none" skips AI (exact sources only).
    backend: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    chat_model: str = "qwen3:4b"
    vision_model: str = "qwen2.5vl:7b"
    timeout_seconds: float = 300.0


@dataclass
class Assistant:
    # "draft": every customer reply waits for the owner. "auto": sent directly.
    mode: str = "draft"
    digest_time: str = "19:00"
    working_hours: str = "09:00-20:00"
    takeover_hours: int = 12
    disclose_bot: bool = True
    max_replies_per_hour: int = 60
    # Messages older than this when the PC comes back online go to the owner
    # instead of getting an automatic reply.
    stale_after_hours: int = 6


@dataclass
class Settings:
    business: Business = field(default_factory=Business)
    owner: Owner = field(default_factory=Owner)
    tally: Tally = field(default_factory=Tally)
    ledgers: Ledgers = field(default_factory=Ledgers)
    ai: AI = field(default_factory=AI)
    assistant: Assistant = field(default_factory=Assistant)
    home: Path = field(default_factory=default_home)

    @property
    def db_path(self) -> Path:
        return self.home / "munshig.db"

    @property
    def media_dir(self) -> Path:
        return self.home / "media"

    @property
    def whatsapp_auth_dir(self) -> Path:
        return self.home / "whatsapp-auth"

    @property
    def knowledge_path(self) -> Path:
        return self.home / "knowledge.md"

    @property
    def config_path(self) -> Path:
        return self.home / "munshig.toml"


_SECTIONS = {
    "business": Business,
    "owner": Owner,
    "tally": Tally,
    "ledgers": Ledgers,
    "ai": AI,
    "assistant": Assistant,
}


def load(home: Path | None = None) -> Settings:
    home = home or default_home()
    settings = Settings(home=home)
    path = settings.config_path
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for name, cls in _SECTIONS.items():
            section = data.get(name, {})
            known = {f.name for f in fields(cls)}
            unknown = set(section) - known
            if unknown:
                raise ValueError(f"Unknown setting(s) in [{name}]: {', '.join(sorted(unknown))}")
            setattr(settings, name, cls(**section))
    return settings


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save(settings: Settings) -> Path:
    settings.home.mkdir(parents=True, exist_ok=True)
    lines = ["# MunshiG settings. Edit with care, then restart MunshiG.", ""]
    for name in _SECTIONS:
        lines.append(f"[{name}]")
        section = getattr(settings, name)
        for f in fields(section):
            lines.append(f"{f.name} = {_toml_value(getattr(section, f.name))}")
        lines.append("")
    settings.config_path.write_text("\n".join(lines), encoding="utf-8")
    return settings.config_path
