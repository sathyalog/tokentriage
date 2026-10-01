"""Routing shared by the provider SDK integrations (Anthropic, OpenAI).

The SDK resource classes are patched (like the LangChain classes), so every client, including
ones created before enable(), is covered. A routed call only changes the `model` argument
(plus settings the routed model would reject); the client, its key and endpoint are untouched.
"""

from __future__ import annotations

import fnmatch
import functools
import inspect
import logging
from contextvars import ContextVar
from typing import Any, Callable

from .. import tracing
from ..providers import OPENROUTER_HOSTS, PROVIDERS, host_of, openrouter_vendor, spec_for

log = logging.getLogger("tokentriage")

# True while a routed call runs, so the SDK call made inside a routed LangChain call (or an SDK
# method that calls another one) is not routed and counted a second time.
ROUTED: ContextVar[bool] = ContextVar("tokentriage_routed", default=False)


_failed: set[str] = set()


def fail_open(where: str, exc: Exception) -> None:
    """Routing itself failed: the call goes to the configured model. Warn the first time, then stay quiet."""
    if where in _failed:
        log.debug("tokentriage: routing failed for %s", where, exc_info=True)
        return
    _failed.add(where)
    log.warning("tokentriage: routing failed for %s calls (%s: %s); they use the configured model instead. "
                "Set log_level: DEBUG for the traceback.", where, type(exc).__name__, exc)
    log.debug("tokentriage: routing failure details", exc_info=True)


def provider_for(client: Any, allowed: frozenset[str], extra_hosts: dict[str, str] | None) -> str | None:
    """Provider served by this client's endpoint, if it is one this SDK may route."""
    host = host_of(str(getattr(client, "base_url", "") or ""))
    if host is None:
        return None
    provider = None
    for configured, name in (extra_hosts or {}).items():
        if host == configured.lower():
            provider = name
            break
    else:
        if any(host == h or host.endswith("." + h) for h in OPENROUTER_HOSTS):
            provider = "openrouter"
        else:
            provider = next((name for name, spec in PROVIDERS.items()
                             if any(host == h or host.endswith("." + h) for h in spec.hosts)), None)
    return provider if provider in allowed else None


def _apply_compat(provider: str, model: str, kwargs: dict) -> None:
    """Adjust settings the routed model would reject (mirrors langchain._compat_updates)."""
    spec = spec_for(provider, model)
    ceiling = spec.max_output if spec else None
    for field in ("max_tokens", "max_completion_tokens"):
        value = kwargs.get(field)
        if ceiling and isinstance(value, int) and value > ceiling:
            kwargs[field] = ceiling
    if provider == "anthropic":
        thinking = kwargs.get("thinking")
        kind = thinking.get("type") if isinstance(thinking, dict) else None
        if model.startswith("claude-haiku") and kind == "adaptive":
            kwargs.pop("thinking")  # Haiku 4.5 has no adaptive thinking
        elif not model.startswith("claude-haiku") and kind == "enabled":
            kwargs["thinking"] = {"type": "adaptive"}  # budget_tokens is rejected on Sonnet 5 / Opus 5.x
    elif provider == "openai" and model.startswith("gpt-4o"):
        kwargs.pop("reasoning_effort", None)
        kwargs.pop("reasoning", None)


def _plan(resource: Any, kwargs: dict, allowed: frozenset[str], enabled: set[str], extract: Callable,
          stream: bool) -> tuple[dict, Any] | None:
    try:
        return _plan_unguarded(resource, kwargs, allowed, enabled, extract, stream)
    except Exception as exc:  # noqa: BLE001 - a routing bug must never break the app's own call
        fail_open(type(resource).__module__.split(".")[0], exc)
        return None


def _plan_unguarded(resource: Any, kwargs: dict, allowed: frozenset[str], enabled: set[str], extract: Callable,
                    stream: bool) -> tuple[dict, Any] | None:
    from . import langchain as lc

    router = lc._State.router
    if router is None or ROUTED.get():
        return None
    model, messages = kwargs.get("model"), kwargs.get("messages")
    if not isinstance(model, str) or messages is None:
        return None
    cfg = router.config
    provider = provider_for(getattr(resource, "_client", None), allowed, cfg.provider_hosts)
    if provider is None or provider not in enabled:
        return None
    if any(fnmatch.fnmatch(model, pat) for pat in cfg.never_route):
        return None
    if provider == "openrouter" and cfg.openrouter_mode == "family" and openrouter_vendor(model) not in cfg.openrouter_families:
        return None

    msgs = list(messages)
    if kwargs.get("system"):  # Anthropic passes the system prompt outside `messages`
        msgs.insert(0, {"role": "system", "content": kwargs["system"]})
    features = extract(msgs, kwargs.get("tools"), kwargs.get("tool_choice"))
    max_out = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
    decision = router.decide(provider, model, features, None, max_out if isinstance(max_out, int) else None)
    routed = dict(kwargs, model=decision.model)
    _apply_compat(provider, decision.model, routed)
    return routed, tracing.begin(decision, features, None, cfg, stream)


def _usage(result: Any) -> dict | None:
    usage = getattr(result, "usage", None)
    if usage is None:
        return None
    tin = getattr(usage, "input_tokens", None)
    tout = getattr(usage, "output_tokens", None)
    if tin is None:  # OpenAI chat completions
        tin, tout = getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0)
    return {"input_tokens": int(tin or 0), "output_tokens": int(tout or 0)}


def _finish(trace: Any, result: Any, error: BaseException | None = None) -> None:
    from . import langchain as lc

    try:
        tracing.end(trace, _usage(result), lc._State.telemetry, error)
    except Exception:  # noqa: BLE001 - observability must never break the app's call
        log.debug("tokentriage: tracing failed", exc_info=True)


async def _await(pending: Any, trace: Any, stream: bool) -> Any:
    token = ROUTED.set(True)
    try:
        result = await pending
    except BaseException as exc:
        _finish(trace, None, exc)
        raise
    finally:
        ROUTED.reset(token)
    if not stream:
        _finish(trace, result)
    return result


def _wrap(orig: Callable, allowed: frozenset[str], enabled: set[str], extract: Callable) -> Callable:
    @functools.wraps(orig)
    def method(self, *args, **kwargs):
        stream = orig.__name__ == "stream" or bool(kwargs.get("stream"))
        planned = None if args else _plan(self, kwargs, allowed, enabled, extract, stream)
        if planned is None:
            return orig(self, *args, **kwargs)
        routed, trace = planned
        token = ROUTED.set(True)
        try:
            result = orig(self, **routed)
        except BaseException as exc:
            _finish(trace, None, exc)
            raise
        finally:
            ROUTED.reset(token)
        if inspect.isawaitable(result):  # async clients return a coroutine from a sync-looking method
            return _await(result, trace, stream)
        if not stream:
            _finish(trace, result)
        # Streams are routed and logged, but their token usage is not recorded yet.
        return result

    method.__tokentriage__ = True
    return method


class SdkPatches:
    """The methods one SDK integration has patched, so they can be restored."""

    def __init__(self, allowed: frozenset[str]):
        self.allowed = allowed
        self.enabled: set[str] = set()
        self.patches: list[tuple[type, str, Any]] = []

    def patch(self, classes: tuple[type, ...], names: tuple[str, ...], extract: Callable) -> None:
        for cls in classes:
            for name in names:
                orig = cls.__dict__.get(name)
                if orig is None or getattr(orig, "__tokentriage__", False):
                    continue
                setattr(cls, name, _wrap(orig, self.allowed, self.enabled, extract))
                self.patches.append((cls, name, orig))

    def restore(self) -> None:
        for cls, name, orig in reversed(self.patches):
            setattr(cls, name, orig)
        self.patches.clear()
        self.enabled.clear()
