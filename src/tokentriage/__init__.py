"""tokentriage: pick the cheapest capable model per request, inside the same provider.

Powered by lev (https://github.com/InterfazeAI/lev) from the InterfazeAI team: every routing
decision is made by lev, and this package is a thin wrapper that applies those decisions to
LangChain chat models.

    import tokentriage
    tokentriage.enable()                       # every supported chat model call is routed

    llm = tokentriage.route(ChatAnthropic(model="claude-opus-5-5"))   # or opt in one instance
"""

from __future__ import annotations

from typing import Any, TypeVar

from . import live, pii, tracing
from . import usage_store as _usage
from .config import DEFAULT_TIERS, PRICES, TIERS, RouterConfig
from .integrations import langchain as _lc
from .providers import ALL_PROVIDERS, PROVIDERS, resolve_provider
from .router import Decision, Router
from .telemetry import Telemetry
from .config_loader import load_config
from .setup_check import validate_setup
from .cli_setup import setup_interactive

# Run setup validation on import
validate_setup()

__all__ = [
    "DEFAULT_TIERS",
    "PRICES",
    "PROVIDERS",
    "TIERS",
    "Decision",
    "Router",
    "RouterConfig",
    "disable",
    "enable",
    "exclude",
    "pii",
    "route",
    "stats",
    "eval_report",
    "usage",
    "usage_report",
    "warmup",
    "load_config",
    "setup_interactive",
]

__version__ = "0.1.0"

_DEFAULT_PROVIDERS = ALL_PROVIDERS

M = TypeVar("M")


def _configure(config: RouterConfig | None, router: Router | None) -> None:
    if router is not None:
        _lc._State.router = router
    elif config is not None or _lc._State.router is None:
        _lc._State.router = Router(config or RouterConfig.from_env())
    cfg = _lc._State.router.config
    if _lc._State.telemetry is None or config is not None or router is not None:
        _lc._State.telemetry = Telemetry(cfg.log_path)
    tracing.setup_logging(cfg, _lc._State.telemetry)
    _setup_usage(cfg)
    if _lc._State.evaluator is not None and _lc._State.evaluator.config is not cfg:
        _lc._State.evaluator.shutdown()
        _lc._State.evaluator = None
    if cfg.mode == "eval" and _lc._State.evaluator is None:
        from .evaluate import Evaluator

        _lc._State.evaluator = Evaluator(cfg, tracing.record_eval)


class _UsageState:
    # The in-memory window lives for the whole process, across reconfiguration.
    memory: _usage.MemoryStore | None = None
    server: live.LiveServer | None = None
    configured_for: int | None = None


def _setup_usage(cfg: RouterConfig) -> None:
    """Wire the last-24h stores for this config (idempotent per config object)."""
    if _UsageState.configured_for == id(cfg):
        return
    _UsageState.configured_for = id(cfg)
    home = _usage.home_dir(cfg.usage_home)
    if _UsageState.memory is None:
        _UsageState.memory = _usage.MemoryStore(cfg.usage_retention_hours, cfg.usage_memory_max)
    files = _usage.HourlyFileStore(home / "usage", cfg.usage_retention_hours) if cfg.usage_file else None
    tracing.set_usage_sink(_usage.UsageSink(_UsageState.memory, files))

    if _UsageState.server is not None:
        _UsageState.server.stop()
        _UsageState.server = None
    if cfg.usage_live:
        server = live.LiveServer(_UsageState.memory, live.run_dir(home))
        if server.start():
            _UsageState.server = server


def _teardown_usage() -> None:
    tracing.set_usage_sink(None)
    if _UsageState.server is not None:
        _UsageState.server.stop()
    _UsageState.server = None
    _UsageState.memory = None
    _UsageState.configured_for = None


def enable(
    config: RouterConfig | None = None,
    *,
    providers: tuple[str, ...] = _DEFAULT_PROVIDERS,
    router: Router | None = None,
    frameworks: tuple[str, ...] = ("langchain",),
) -> list[str]:
    """Route every supported chat model call in this process.

    providers: which providers to route (default: all, including "openrouter"). Returns the
    ones whose LangChain package is installed.
    
    frameworks: which SDK frameworks to enable routing for (default: ["langchain"]).
    Supports: "langchain", "anthropic", "openai".
    """
    _configure(config, router)
    
    installed = []
    
    # Install each framework's routing
    for framework in frameworks:
        if framework == "langchain":
            _lc._State.route_all = True
            installed.extend(_lc.install(providers))
        elif framework == "anthropic":
            from .integrations import anthropic_sdk
            installed.extend(anthropic_sdk.install(providers))
            anthropic_sdk._State.router = _lc._State.router
        elif framework == "openai":
            from .integrations import openai_sdk
            installed.extend(openai_sdk.install(providers))
            openai_sdk._State.router = _lc._State.router
        else:
            raise ValueError(f"Unknown framework: {framework}. Choose from: langchain, anthropic, openai")
    
    return installed


def route(llm: M, config: RouterConfig | None = None, *, router: Router | None = None) -> M:
    """Opt one chat model instance into routing and return it (same object).

    `llm.bind_tools(...)` and `llm.with_structured_output(...)` stay routed.
    """
    target = _lc.target_for(llm)
    if target is None:
        supported = ", ".join(sorted(_lc.TARGETS))
        raise TypeError(f"tokentriage.route() supports {supported}; got {type(llm).__name__}")
    _configure(config, router)
    provider, why = resolve_provider(llm, target, _lc._State.router.config.provider_hosts)
    if provider is None:
        raise ValueError(
            f"cannot route this {target.class_name}: {why}. "
            "Add the host to RouterConfig(provider_hosts={...}) if it serves a known provider."
        )
    _lc.install((provider,))
    _lc._track(_lc._State.opt_in, llm)
    return llm


def exclude(llm: Any) -> Any:
    """Never route this instance, even after enable(). Returns it."""
    _lc._track(_lc._State.excluded, llm)
    return llm


def disable() -> None:
    """Restore the original chat model methods and stop the live usage socket."""
    _lc.uninstall()
    _teardown_usage()


def stats() -> dict:
    """Calls, models chosen, and actual vs baseline cost since enable()."""
    return _lc._State.telemetry.summary() if _lc._State.telemetry else {}


def usage(since: str | None = "24h", by: str = "model", user: str | None = None, task: str | None = None) -> dict:
    """This process's token usage and estimated cost from memory (last 24h at most).

    by: "model" | "provider" | "tier" | "task" | "user". Filter with user= (the value passed as
    metadata={"tokentriage_user": ...}) to show an end user their own usage in your app's UI.
    """
    store = _UsageState.memory
    if store is None:
        return _usage.aggregate([], by=by)
    start, note = _usage.parse_since(since, store.window_s / 3600)
    report = _usage.aggregate(store.query(start), by=by, user=user, task=task)
    if note:
        report["note"] = note
    return report


def eval_report(since: str | None = "24h", by: str = "tier", user: str | None = None, task: str | None = None) -> dict:
    """Quality of routing from this process's evaluated calls (mode="eval"): held %, scores, savings."""
    store = _UsageState.memory
    if store is None:
        return _usage.aggregate_eval([], by=by)
    start, _ = _usage.parse_since(since, store.window_s / 3600)
    return _usage.aggregate_eval(store.query(start), by=by, user=user, task=task)


def usage_report(since: str | None = "24h", by: str = "model", user: str | None = None, task: str | None = None) -> str:
    """usage() as a printable table."""
    return _usage.format_table(usage(since, by, user, task), title=f"tokentriage usage, last {since or '24h'} (this process)")


def warmup() -> None:
    """Load lev now (backend='lev-local'), so the first request is not delayed."""
    if _lc._State.router is None:
        _configure(None, None)
    classifier = _lc._State.router.classifier
    if hasattr(classifier, "warmup"):
        classifier.warmup()
