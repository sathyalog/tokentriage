"""Route OpenAI SDK calls by wrapping the client's chat.completions."""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..converters.openai import extract as extract_openai
from ..router import Router
from ..tracing import CallTrace

log = logging.getLogger("tokentriage")

_metadata_lock = threading.Lock()
_metadata_cache: dict[str, dict] = {}


class _State:
    router: Router | None = None
    route_all = False
    in_flight: set[int] = set()
    patched: bool = False


def _route_request(
    client: Any, model: str, messages: list, tools: list | None = None, **kwargs
) -> tuple[Any, str, CallTrace | None]:
    """Decide routing and return (routed_client, routed_model, trace)."""
    if _State.router is None:
        return client, model, None

    try:
        features = extract_openai(messages, tools, kwargs.get("tool_choice"))
        decision = _State.router.decide("openai", model, features)
        trace = CallTrace("route", decision.model, decision.tier)

        if decision.model != model:
            routed_client = client.model_copy(update={"api_key": client.api_key})
            return routed_client, decision.model, trace
        
        return client, model, trace

    except Exception as e:
        log.debug("routing failed: %s", e)
        return client, model, None


def install(providers: tuple[str, ...]) -> list[str]:
    """Install routing patches for OpenAI SDK."""
    if "openai" not in providers:
        return []

    try:
        from openai import OpenAI, AsyncOpenAI
        
        # Store original methods
        original_openai_init = OpenAI.__init__
        original_async_init = AsyncOpenAI.__init__
        
        def patched_openai_init(self, **kwargs):
            original_openai_init(self, **kwargs)
            
            # Store the original completions object
            original_completions = self.chat.completions
            
            # Create wrapper that intercepts create calls
            class WrappedCompletions:
                def __init__(self, client, orig_completions):
                    self._client = client
                    self._orig = orig_completions
                
                def create(self_wrapped, *, model, messages, tools=None, stream=False, **kwargs):
                    call_id = id(self_wrapped._client)
                    
                    if call_id in _State.in_flight:
                        return self_wrapped._orig.create(model=model, messages=messages, tools=tools, stream=stream, **kwargs)
                    
                    routed_client, routed_model, trace = _route_request(
                        self_wrapped._client, model, messages, tools, **kwargs
                    )
                    
                    try:
                        _State.in_flight.add(id(routed_client))
                        result = routed_client.chat.completions._orig.create(
                            model=routed_model, messages=messages, tools=tools, stream=stream, **kwargs
                        )
                        
                        if trace:
                            with _metadata_lock:
                                _metadata_cache[str(id(result))] = {
                                    "model": trace.model,
                                    "tier": trace.tier,
                                    "routed_from": model,
                                }
                        
                        return result
                    finally:
                        _State.in_flight.discard(id(routed_client))
                
                def __getattr__(self_wrapped, name):
                    return getattr(self_wrapped._orig, name)
            
            self.chat.completions = WrappedCompletions(self, original_completions)
        
        def patched_async_init(self, **kwargs):
            original_async_init(self, **kwargs)
            
            original_completions = self.chat.completions
            
            class AsyncWrappedCompletions:
                def __init__(self_wrapped, client, orig_completions):
                    self_wrapped._client = client
                    self_wrapped._orig = orig_completions
                
                async def create(self_wrapped, *, model, messages, tools=None, stream=False, **kwargs):
                    call_id = id(self_wrapped._client)
                    
                    if call_id in _State.in_flight:
                        return await self_wrapped._orig.create(model=model, messages=messages, tools=tools, stream=stream, **kwargs)
                    
                    routed_client, routed_model, trace = _route_request(
                        self_wrapped._client, model, messages, tools, **kwargs
                    )
                    
                    try:
                        _State.in_flight.add(id(routed_client))
                        result = await routed_client.chat.completions._orig.create(
                            model=routed_model, messages=messages, tools=tools, stream=stream, **kwargs
                        )
                        
                        if trace:
                            with _metadata_lock:
                                _metadata_cache[str(id(result))] = {
                                    "model": trace.model,
                                    "tier": trace.tier,
                                    "routed_from": model,
                                }
                        
                        return result
                    finally:
                        _State.in_flight.discard(id(routed_client))
                
                def __getattr__(self_wrapped, name):
                    return getattr(self_wrapped._orig, name)
            
            self.chat.completions = AsyncWrappedCompletions(self, original_completions)
        
        OpenAI.__init__ = patched_openai_init
        AsyncOpenAI.__init__ = patched_async_init
        
        log.info("installed openai sdk routing")
        return ["openai"]

    except ImportError:
        log.debug("openai not installed; skipping openai sdk routing")
        return []


def get_metadata(response_id: str) -> dict | None:
    """Retrieve routing metadata for a response."""
    with _metadata_lock:
        return _metadata_cache.pop(response_id, None)


def uninstall() -> None:
    """Uninstall patches (for testing)."""
    _metadata_cache.clear()
