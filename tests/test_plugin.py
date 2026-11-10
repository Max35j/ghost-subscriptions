"""The Hermes wiring, checked with a fake ctx (Hermes itself is not installed for these tests)."""
import importlib.util
import inspect
import json
import re
import sys

import pytest

from conftest import BINARY_SAMPLES, FIXTURES, ROOT

DEMO = ROOT / "examples" / "revolut-12-months.csv"


def load_plugin():
    name = "statement_audit_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    def __init__(self, settings=None):
        self.tools, self.commands, self.skills, self.hooks = {}, {}, {}, []
        self.settings = settings or {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (schema, handler)

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_skill(self, name, path, description=""):
        self.skills[name] = path

    def register_hook(self, *a, **k):
        self.hooks.append(a)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


@pytest.fixture
def plugin(tmp_path):
    mod = load_plugin()
    mod._TEST_DATA_DIR = tmp_path / "plugin-data"
    yield mod
    mod._TEST_DATA_DIR = None


def manifest_list(key):
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    block = re.search(rf"^{key}:\n((?:  - .+\n)+)", text, re.M)
    return [ln.strip()[2:] for ln in block.group(1).splitlines()] if block else []


def test_registered_surfaces_match_the_manifest(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    assert sorted(ctx.tools) == sorted(manifest_list("provides_tools"))
    assert ctx.hooks == []
    assert list(ctx.commands) == ["subs"]
    assert list(ctx.skills) == ["monthly-audit"] and ctx.skills["monthly-audit"].is_file()
    for schema, handler in ctx.tools.values():
        params = inspect.signature(handler).parameters
        assert any(p.kind is p.VAR_KEYWORD for p in params.values())
        assert "Use it" in schema["description"] or "Use this" in schema["description"]


def test_handlers_return_json_strings_and_never_raise(plugin, tmp_path):
    ctx = FakeCtx({"holder_aliases": ["Alex Martin"]})
    plugin.register(ctx)
    tool = {n: h for n, (s, h) in ctx.tools.items()}
    for name, args in [("statement_import", {}), ("statement_import", {"path": str(FIXTURES / "empty.csv")}),
                       ("statement_audit", {}), ("statement_cancel_pack", {"ids": ["x"]}),
                       ("statement_purge", {}), ("statement_audit", {"from": "nonsense"}),
                       ("statement_import", None)]:
        out = tool[name](args)
        assert isinstance(out, str) and "error" in json.loads(out), (name, out)

    imp = json.loads(tool["statement_import"]({"path": str(DEMO)}, task_id="t1"))
    assert imp["profile_used"] == "revolut"
    audit = json.loads(tool["statement_audit"]({"as_of": "2026-11-09"}))
    assert audit["totals"]["count"] == 14 and audit["excluded"]["internal_transfers"] == 12
    lines = ctx.commands["subs"]("").splitlines()
    assert len(lines) == 3
    pack = json.loads(tool["statement_cancel_pack"]({"ids": "netflix,audible", "as_of": "2026-11-09"}))
    assert pack["status"] == "written" and pack["folder"].startswith(str(plugin._TEST_DATA_DIR))
    gone = json.loads(tool["statement_purge"]({"confirm": True}))
    assert gone["status"] == "purged" and gone["files_deleted"] == 3
    assert gone["database_removed"] is True and list(plugin._TEST_DATA_DIR.iterdir()) == []
    assert "No statement" in ctx.commands["subs"]("")


def test_internal_errors_become_json(plugin, monkeypatch):
    ctx = FakeCtx()
    plugin.register(ctx)
    monkeypatch.setattr(plugin.service, "run_audit", lambda *a, **k: 1 / 0)
    out = json.loads(ctx.tools["statement_audit"][1]({}))
    assert "ZeroDivisionError" in out["error"]
    assert "could not build" in ctx.commands["subs"]("")


def test_settings_tolerate_bad_types(plugin):
    s = plugin.Settings(FakeCtx({"holder_aliases": "Alex Martin, A MARTIN", "fx_rates": "oops"}))
    assert s.get("holder_aliases") == ["Alex Martin", "A MARTIN"]
    assert s.get("fx_rates") == {}
    assert plugin.Settings(None).get("language") == "en"


def test_no_network_or_forbidden_constructs_in_shipped_code():
    banned = re.compile(r"\b(socket|urllib|requests|httpx|http\.client|subprocess|eval|exec|setattr|"
                        r"sys\.modules|os\.environ|getenv|shutil\.rmtree)\b|\.env\b|auth\.json")
    for path in [ROOT / "__init__.py", ROOT / "schemas.py", *sorted((ROOT / "engine").glob("*.py"))]:
        code = re.sub(r'"""[\s\S]*?"""', "", path.read_text(encoding="utf-8"))
        hits = [ln for ln in code.splitlines() if banned.search(ln) and not ln.strip().startswith("#")]
        assert not hits, (path.name, hits)
