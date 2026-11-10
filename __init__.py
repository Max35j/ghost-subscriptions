"""ghost-subscriptions: Hermes plugin registration.

Wires four tools, the /subs slash command and a bundled skill to the pure engine in
``engine/``. Every handler returns a JSON string and never raises.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import schemas
from .engine import service

logger = logging.getLogger(__name__)

PLUGIN_NAME = "ghost-subscriptions"
_PLUGIN_DIR = Path(__file__).resolve().parent

# Tests point these at a temporary folder; in Hermes they stay None and the
# sanctioned per-plugin storage (plugins.plugin_storage) is used.
_TEST_DATA_DIR: Path | None = None

DEFAULTS = {
    "confidence_threshold": 0.6, "language": "en", "holder_aliases": [], "holder_name": "",
    "column_mapping": {}, "fx_rates": {}, "contract_refs": {},
}


# --------------------------------------------------------------------------- storage

def _data_dir() -> Path:
    if _TEST_DATA_DIR is not None:
        _TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
        return _TEST_DATA_DIR
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_NAME)


def _connect() -> sqlite3.Connection:
    if _TEST_DATA_DIR is not None:
        return sqlite3.connect(_data_dir() / "data.db")
    from plugins.plugin_storage import plugin_db
    return plugin_db(PLUGIN_NAME)


def _session_cwd() -> Path:
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return Path(resolve_agent_cwd())
    except Exception:
        return Path.cwd()


def _resolve_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    return p if p.is_absolute() else _session_cwd() / p


# --------------------------------------------------------------------------- helpers

def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=None, separators=(",", ": "))


def _safe(fn: Callable[..., dict]) -> Callable[..., str]:
    """Wrap a handler: JSON string out, no exception ever escapes."""
    def handler(args: dict | None = None, **kwargs) -> str:
        try:
            return _dump(fn(args or {}, **kwargs))
        except Exception as exc:                     # last line of defence; details go to the log
            logger.exception("ghost-subscriptions: %s failed", fn.__name__)
            return _dump({"error": f"Internal error in {fn.__name__}: {type(exc).__name__}: {exc}. "
                                   "Your data is unchanged; please report this with the message above."})
    handler.__name__ = fn.__name__
    return handler


class Settings:
    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    def get(self, key: str) -> Any:
        default = DEFAULTS[key]
        try:
            value = self._ctx.get_config(key, default=default) if self._ctx is not None else default
        except Exception:
            value = default
        if value is None:
            return default
        if isinstance(default, list) and isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(default, (list, dict)) and not isinstance(value, type(default)):
            return default
        return value

    def audit_kwargs(self) -> dict:
        return {"threshold": self.get("confidence_threshold"), "holder_aliases": self.get("holder_aliases"),
                "fx_rates": self.get("fx_rates")}


# --------------------------------------------------------------------------- handlers

def make_handlers(settings: Settings) -> dict[str, Callable[..., str]]:
    def statement_import(args: dict, **_: Any) -> dict:
        raw = str(args.get("path") or "").strip()
        if not raw:
            return {"error": "Give the path of the CSV or OFX file exported from your bank."}
        path = _resolve_path(raw)
        profile = str(args.get("profile") or "").strip() or None
        with closing(_connect()) as conn:
            return service.import_statement(conn, path, profile, settings.get("column_mapping"))

    def statement_audit(args: dict, **_: Any) -> dict:
        kw = settings.audit_kwargs()
        if args.get("confidence_threshold") not in (None, ""):
            kw["threshold"] = args["confidence_threshold"]
        with closing(_connect()) as conn:
            return service.run_audit(conn, period_from=args.get("from"), period_to=args.get("to"),
                                     as_of=args.get("as_of"), **kw)

    def statement_cancel_pack(args: dict, **_: Any) -> dict:
        ids = args.get("ids") or []
        if isinstance(ids, str):
            ids = [i.strip() for i in ids.split(",") if i.strip()]
        formats = args.get("formats") or None
        if isinstance(formats, str):
            formats = [f.strip() for f in formats.split(",") if f.strip()]
        with closing(_connect()) as conn:
            return service.cancel_pack(
                conn, _data_dir(), [str(i) for i in ids], language=args.get("language") or settings.get("language"),
                formats=formats, holder_name=settings.get("holder_name"), contract_refs=settings.get("contract_refs"),
                as_of=args.get("as_of"), **settings.audit_kwargs())

    def statement_purge(args: dict, **_: Any) -> dict:
        if args.get("confirm") is not True:
            return {"error": "Nothing deleted. Ask the user to confirm, then call statement_purge with confirm=true."}
        data_dir = _data_dir()
        with closing(_connect()) as conn:
            result = service.purge(conn, data_dir)
        # The emptied database file goes too, so the data folder is left empty.
        leftovers = [data_dir / n for n in ("data.db", "data.db-wal", "data.db-shm")]
        for f in leftovers:
            try:
                f.unlink()
            except OSError:
                pass
        result["database_removed"] = not any(f.exists() for f in leftovers)
        return result

    return {f.__name__: _safe(f) for f in (statement_import, statement_audit, statement_cancel_pack, statement_purge)}


def make_subs_command(settings: Settings) -> Callable[..., str]:
    def subs(raw_args: str = "", **_: Any) -> str:
        try:
            with closing(_connect()) as conn:
                result = service.run_audit(conn, **settings.audit_kwargs())
            return service.summary_lines(result, settings.get("language"))
        except Exception as exc:
            logger.exception("ghost-subscriptions: /subs failed")
            return f"ghost-subscriptions: could not build the summary ({type(exc).__name__}: {exc})."
    return subs


# --------------------------------------------------------------------------- registration

def register(ctx) -> None:
    settings = Settings(ctx)
    handlers = make_handlers(settings)
    for schema in schemas.ALL:
        ctx.register_tool(name=schema["name"], toolset=PLUGIN_NAME, schema=schema, handler=handlers[schema["name"]],
                          description=schema["description"].split(". ")[0] + ".", emoji="👻")
    ctx.register_command("subs", handler=make_subs_command(settings),
                         description="Three-line summary of your subscriptions (ghost-subscriptions)")
    ctx.register_skill("monthly-audit", _PLUGIN_DIR / "skill" / "SKILL.md",
                       description="How to run a monthly subscription audit from a bank statement and report it honestly.")
