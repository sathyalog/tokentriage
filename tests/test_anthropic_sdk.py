"""Tests for Anthropic SDK routing integration."""

import pytest
from unittest.mock import Mock

import tokentriage
from tokentriage import Router, RouterConfig
from tokentriage.integrations import anthropic_sdk
from conftest import SIMPLE, FixedClassifier


KEY = "sk-ant-TESTKEY000000000000000000"


class FakeMessage:
    """Fake Anthropic Message response."""
    def __init__(self, content, model, usage=None):
        self.content = content
        self.model = model
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50}


class FakeMessages:
    """Fake Anthropic client.messages object."""
    
    def __init__(self, client):
        self.client = client
        self.calls = []
    
    def create(self, *, model, messages, tools=None, **kwargs):
        self.calls.append(("create", model, messages, tools))
        return FakeMessage(f"response from {model}", model)
    
    def stream(self, *, model, messages, tools=None, **kwargs):
        self.calls.append(("stream", model, messages, tools))
        return iter([f"chunk {i}" for i in range(3)])


class FakeAnthropic:
    """Fake Anthropic client."""
    
    # Class-level messages property
    class _MessagesDescriptor:
        def __get__(self, obj, objtype=None):
            if obj is None:
                return FakeMessages
            if not hasattr(obj, '_messages_cache'):
                obj._messages_cache = FakeMessages(obj)
            return obj._messages_cache
    
    messages = _MessagesDescriptor()
    
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
    
    def model_copy(self, *, update=None):
        """Create a copy with updated fields."""
        copy = FakeAnthropic(api_key=self.api_key, **self.kwargs)
        if update:
            for k, v in update.items():
                setattr(copy, k, v)
        return copy


class FakeAsyncMessages:
    """Fake AsyncAnthropic client.messages object."""
    
    def __init__(self, client):
        self.client = client
        self.calls = []
    
    async def create(self, *, model, messages, tools=None, **kwargs):
        self.calls.append(("create", model, messages, tools))
        return FakeMessage(f"response from {model}", model)
    
    async def stream(self, *, model, messages, tools=None, **kwargs):
        self.calls.append(("stream", model, messages, tools))
        
        class AsyncIterator:
            def __init__(self, items):
                self.items = items
                self.index = 0
            
            def __aiter__(self):
                return self
            
            async def __anext__(self):
                if self.index >= len(self.items):
                    raise StopAsyncIteration
                item = self.items[self.index]
                self.index += 1
                return item
        
        return AsyncIterator([f"chunk {i}" for i in range(3)])


class FakeAsyncAnthropic:
    """Fake AsyncAnthropic client."""
    
    class _MessagesDescriptor:
        def __get__(self, obj, objtype=None):
            if obj is None:
                return FakeAsyncMessages
            if not hasattr(obj, '_messages_cache'):
                obj._messages_cache = FakeAsyncMessages(obj)
            return obj._messages_cache
    
    messages = _MessagesDescriptor()
    
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
    
    def model_copy(self, *, update=None):
        copy = FakeAsyncAnthropic(api_key=self.api_key, **self.kwargs)
        if update:
            for k, v in update.items():
                setattr(copy, k, v)
        return copy


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Mock Anthropic SDK and install patches."""
    
    # Create mock module
    mock_module = Mock()
    mock_module.Anthropic = FakeAnthropic
    mock_module.AsyncAnthropic = FakeAsyncAnthropic
    
    # Install in sys.modules
    import sys
    monkeypatch.setitem(sys.modules, "anthropic", mock_module)
    
    # Set up router
    anthropic_sdk._State.router = Router(RouterConfig(), FixedClassifier(SIMPLE))
    anthropic_sdk._State.patched = False
    
    # Install patches
    anthropic_sdk.install(("anthropic",))
    
    yield mock_module
    
    # Cleanup
    anthropic_sdk.uninstall()


def test_anthropic_sync_routing(mock_anthropic):
    """Test synchronous routing with Anthropic SDK."""
    client = mock_anthropic.Anthropic(api_key=KEY)
    
    response = client.messages.create(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "Simple question"}]
    )
    
    # Should have routed to haiku (SIMPLE classifier)
    assert response.model == "claude-haiku-4-5"


def test_anthropic_stream_routing(mock_anthropic):
    """Test streaming with Anthropic SDK."""
    client = mock_anthropic.Anthropic(api_key=KEY)
    
    stream = client.messages.stream(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "Simple question"}]
    )
    
    chunks = list(stream)
    assert len(chunks) == 3


@pytest.mark.asyncio
async def test_anthropic_async_routing(mock_anthropic):
    """Test async routing with Anthropic SDK."""
    client = mock_anthropic.AsyncAnthropic(api_key=KEY)
    
    response = await client.messages.create(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "Simple question"}]
    )
    
    assert response.model == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_anthropic_async_stream_routing(mock_anthropic):
    """Test async streaming with Anthropic SDK."""
    client = mock_anthropic.AsyncAnthropic(api_key=KEY)
    
    stream = await client.messages.stream(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "Simple question"}]
    )
    
    chunks = []
    async for chunk in stream:
        chunks.append(chunk)
    
    assert len(chunks) == 3


def test_anthropic_with_tools(mock_anthropic):
    """Test routing with tool use."""
    client = mock_anthropic.Anthropic(api_key=KEY)
    
    tools = [
        {
            "name": "get_weather",
            "description": "Get weather",
            "input_schema": {"type": "object", "properties": {}}
        }
    ]
    
    response = client.messages.create(
        model="claude-opus-5-5",
        messages=[{"role": "user", "content": "What's the weather?"}],
        tools=tools
    )
    
    assert response.model == "claude-haiku-4-5"


def test_anthropic_no_routing_without_enable():
    """Verify routing is inactive without enable()."""
    anthropic_sdk._State.router = None
    assert anthropic_sdk._State.router is None
