"""Route Anthropic SDK calls by wrapping the messages property."""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..converters.anthropic import extract as extract_anthropic
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
        features = extract_anthropic(messages, tools, kwargs.get("tool_choice"))
        decision = _State.router.decide("anthropic", model, features)
        trace = CallTrace("route", decision.model, decision.tier)

        if decision.model != model:
            routed_client = client.model_copy(update={"api_key": client.api_key})
            return routed_client, decision.model, trace
        
        return client, model, trace

    except Exception as e:
        log.debug("routing failed: %s", e)
        return client, model, None


class _WrappedMessages:
    """Wraps messages object to intercept create/stream calls."""
    
    def __init__(self, client: Any, messages_obj: Any):
        self.client = client
        self._messages = messages_obj
    
    def create(self, *, model: str, messages: list, tools=None, **kwargs):
        call_id = id(self.client)
        
        if call_id in _State.in_flight:
            return self._messages.create(model=model, messages=messages, tools=tools, **kwargs)
        
        routed_client, routed_model, trace = _route_request(self.client, model, messages, tools, **kwargs)
        
        try:
            _State.in_flight.add(id(routed_client))
            result = routed_client.messages.create(model=routed_model, messages=messages, tools=tools, **kwargs)
            
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
    
    def stream(self, *, model: str, messages: list, tools=None, **kwargs):
        call_id = id(self.client)
        
        if call_id in _State.in_flight:
            return self._messages.stream(model=model, messages=messages, tools=tools, **kwargs)
        
        routed_client, routed_model, trace = _route_request(self.client, model, messages, tools, **kwargs)
        
        try:
            _State.in_flight.add(id(routed_client))
            stream = routed_client.messages.stream(model=routed_model, messages=messages, tools=tools, **kwargs)
            
            if trace:
                with _metadata_lock:
                    _metadata_cache[str(id(stream))] = {
                        "model": trace.model,
                        "tier": trace.tier,
                        "routed_from": model,
                    }
            
            return stream
        finally:
            _State.in_flight.discard(id(routed_client))
    
    def __getattr__(self, name):
        return getattr(self._messages, name)


class _AsyncWrappedMessages:
    """Wraps async messages object to intercept create/stream calls."""
    
    def __init__(self, client: Any, messages_obj: Any):
        self.client = client
        self._messages = messages_obj
    
    async def create(self, *, model: str, messages: list, tools=None, **kwargs):
        call_id = id(self.client)
        
        if call_id in _State.in_flight:
            return await self._messages.create(model=model, messages=messages, tools=tools, **kwargs)
        
        routed_client, routed_model, trace = _route_request(self.client, model, messages, tools, **kwargs)
        
        try:
            _State.in_flight.add(id(routed_client))
            result = await routed_client.messages.create(model=routed_model, messages=messages, tools=tools, **kwargs)
            
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
    
    async def stream(self, *, model: str, messages: list, tools=None, **kwargs):
        call_id = id(self.client)
        
        if call_id in _State.in_flight:
            async for item in self._messages.stream(model=model, messages=messages, tools=tools, **kwargs):
                yield item
            return
        
        routed_client, routed_model, trace = _route_request(self.client, model, messages, tools, **kwargs)
        
        try:
            _State.in_flight.add(id(routed_client))
            async for item in routed_client.messages.stream(model=routed_model, messages=messages, tools=tools, **kwargs):
                if trace:
                    with _metadata_lock:
                        _metadata_cache[str(id(item))] = {
                            "model": trace.model,
                            "tier": trace.tier,
                            "routed_from": model,
                        }
                yield item
        finally:
            _State.in_flight.discard(id(routed_client))
    
    def __getattr__(self, name):
        return getattr(self._messages, name)


def install(providers: tuple[str, ...]) -> list[str]:
    """Install routing patches for Anthropic SDK."""
    if "anthropic" not in providers:
        return []

    if _State.patched:
        return ["anthropic"]

    try:
        from anthropic import Anthropic, AsyncAnthropic
        
        # Store original __init__ methods
        original_init = Anthropic.__init__
        original_async_init = AsyncAnthropic.__init__
        
        def patched_init(self, **kwargs):
            original_init(self, **kwargs)
            # Wrap the messages property
            self._original_messages = self.messages
            self.messages = _WrappedMessages(self, self._original_messages)
        
        def patched_async_init(self, **kwargs):
            original_async_init(self, **kwargs)
            self._original_messages = self.messages
            self.messages = _AsyncWrappedMessages(self, self._original_messages)
        
        Anthropic.__init__ = patched_init
        AsyncAnthropic.__init__ = patched_async_init
        
        _State.patched = True
        log.info("installed anthropic sdk routing")
        return ["anthropic"]

    except ImportError:
        log.debug("anthropic not installed; skipping anthropic sdk routing")
        return []


def get_metadata(response_id: str) -> dict | None:
    """Retrieve routing metadata for a response."""
    with _metadata_lock:
        return _metadata_cache.pop(response_id, None)


def uninstall() -> None:
    """Uninstall patches (for testing)."""
    _State.patched = False
    _metadata_cache.clear()
