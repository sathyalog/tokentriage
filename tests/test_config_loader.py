"""load_config must honour the YAML (router settings and models), and lev_download must report real progress."""

import yaml

from tokentriage import cli_setup, lev_download
from tokentriage.config_loader import load_config
from tokentriage.integrations import langchain as lc

YAML = """
enabled: true
router:
  backend: heuristic
  timeout_s: 60.0
  block_on_load: true
models:
  simple:
    providers: [anthropic/claude-haiku-4-5-20251001]
  complex:
    providers: [openrouter/anthropic/claude-opus-5.5, not-a-provider/x]
features:
  track_usage: true
"""


def _load(tmp_path, text, **kwargs):
    path = tmp_path / "tokentriage.yaml"
    path.write_text(text)
    return load_config(str(path), **kwargs)[0]


def test_router_settings_and_models_are_applied(tmp_path, capsys):
    cfg = _load(tmp_path, YAML, auto_enable=False)
    assert cfg.backend == "heuristic" and cfg.timeout_s == 60.0 and cfg.block_on_load is True
    assert cfg.tiers["anthropic"]["simple"] == "claude-haiku-4-5-20251001"
    assert cfg.tiers["anthropic"]["standard"] == "claude-sonnet-5"  # untouched tiers keep defaults
    assert cfg.openrouter_families["anthropic"]["complex"] == "anthropic/claude-opus-5.5"
    out = capsys.readouterr().out
    assert "'not-a-provider/x' ignored" in out and "ignoring config keys" in out and "features" in out


def test_enabled_false_does_not_route(tmp_path, capsys):
    _load(tmp_path, "enabled: false\nrouter:\n  backend: heuristic\n")
    assert lc._State.router is None
    assert "disabled in config" in capsys.readouterr().out


def test_setup_wizard_output_loads(tmp_path, monkeypatch):
    # heuristic backend, OpenRouter, anthropic vendor, no ladder, customise tiers but keep defaults, usage on,
    # no debug, save
    vendor = list(cli_setup.OPENROUTER_FAMILIES).index("anthropic") + 1
    answers = iter(["3", str(len(cli_setup.PROVIDERS) + 1), str(vendor), "n", "y", "", "", "", "y", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    config = cli_setup.setup_interactive()
    cfg = _load(tmp_path, yaml.dump(config), auto_enable=False)
    assert cfg.openrouter_families["anthropic"]["simple"] == "anthropic/claude-haiku-4.5"


def _wizard(monkeypatch, answers):
    monkeypatch.setattr(cli_setup, "_is_lev_installed", lambda: True)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_: next(it))
    return cli_setup.setup_interactive()


def test_setup_wizard_lev_local_preloads(monkeypatch):
    anthropic = str(list(cli_setup.PROVIDERS).index("anthropic") + 1)
    config = _wizard(monkeypatch, ["1", anthropic, "n", "y", "n", "y"])
    assert config["router"]["backend"] == "lev-local" and config["router"]["block_on_load"] is True


def test_setup_wizard_lev_http_asks_for_url(tmp_path, monkeypatch):
    anthropic = str(list(cli_setup.PROVIDERS).index("anthropic") + 1)
    config = _wizard(monkeypatch, ["2", anthropic, "https://lev.example.com", "n", "y", "n", "y"])
    cfg = _load(tmp_path, yaml.dump(config), auto_enable=False)
    assert cfg.backend == "lev-http" and cfg.lev_url == "https://lev.example.com" and cfg.allow_remote_lev is True


def test_setup_wizard_openrouter_ladder(tmp_path, monkeypatch):
    vendor = str(list(cli_setup.OPENROUTER_FAMILIES).index("anthropic") + 1)
    openrouter = str(len(cli_setup.PROVIDERS) + 1)
    config = _wizard(monkeypatch, ["3", openrouter, vendor, "y", "anthropic, openai, nope", "n", "y", "n", "y"])
    cfg = _load(tmp_path, yaml.dump(config), auto_enable=False)
    assert cfg.openrouter_mode == "ladder" and cfg.openrouter_vendors == ("anthropic", "openai")


def test_progress_counts_only_byte_bars(capsys):
    progress = lev_download._Progress("base model", total=200)
    byte_bar = progress.bar_class()(unit="B", total=200)
    count_bar = progress.bar_class()(unit="it", total=3)
    count_bar.update(1)
    assert progress.done == 0
    byte_bar.update(50)
    assert progress.done == 50 and "25.0%" in capsys.readouterr().out
    byte_bar.update(150)
    assert progress.done == 200 and "100.0%" in capsys.readouterr().out


def test_failed_download_reports_false(monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise OSError("no network")

    monkeypatch.setattr(lev_download, "_fetch", boom)
    assert lev_download.ensure_lev_cached("interfaze-ai/lev") is False
    assert "no network" in capsys.readouterr().out
