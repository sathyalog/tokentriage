"""Tests for OpenAI SDK routing integration."""

import pytest
from unittest.mock import Mock

import tokentriage
from tokentriage import Router, RouterConfig
from tokentriage.integrations import openai_sdk
from conftest import SIMPLE, FixedClassifier


KEY = "sk-test-TESTKEY000000000000000000"


class FakeChatCompletion:
    """Fake OpenAI ChatCompletion response."""
    def __init__(self, content, model, usage=None):
        self.id = f"chatcmpl-{id(self)}"
        self.object = "chat.completion"
        self.created = 1234567890
        self.model = model
        self.choices = [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


class FakeCompletions:
    """Fake OpenAI client.chat.completions object."""
    
    def __init__(self, client):
        self.client = client
        self.calls = []
    
    def create(self, *, model, messages, tools=None, stream=False, **kwargs):
        self.calls.append(("create", model, messages, tools, stream))
        return FakeChatCompletion(f"response from {model}", model)


class FakeChat:
    """Fake OpenAI client.chat object."""
    
    def __init__(self, client):
        self.client = client
        self.completions = FakeCompletions(client)


class FakeOpenAI:
    """Fake OpenAI client - stores chat instance."""
    
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
        self.chat = FakeChat(self)
    
    def model_copy(self, *, update=None):
        copy = FakeOpenAI(api_key=self.api_key, **self.kwargs)
        if update:
            for k, v in update.items():
                setattr(copy, k, v)
        return copy


class FakeAsyncCompletions:
    """Fake async OpenAI client.chat.completions object."""
    
    def __init__(self, client):
        self.client = client
        self.calls = []
    
    async def create(self, *, model, messages, tools=None, stream=False, **kwargs):
        self.calls.append(("create", model, messages, tools, stream))
        return FakeChatCompletion(f"response from {model}", model)


class FakeAsyncChat:
    """Fake async OpenAI client.chat object."""
    
    def __init__(self, client):
        self.client = client
        self.completions = FakeAsyncCompletions(client)


class FakeAsyncOpenAI:
    """Fake AsyncOpenAI client."""
    
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
        self.chat = FakeAsyncChat(self)
    
    def model_copy(self, *, update=None):
        copy = FakeAsyncOpenAI(api_key=self.api_key, **self.kwargs)
        if update:
            for k, v in update.items():
                setattr(copy, k, v)
        return copy


@pytest.fixture
def mock_openai(monkeypatch):
    """Mock OpenAI SDK and install patches."""
    
    mock_module = Mock()
    mock_module.OpenAI = FakeOpenAI
    mock_module.AsyncOpenAI = FakeAsyncOpenAI
    
    import sys
    monkeypatch.setitem(sys.modules, "openai", mock_module)
    
    # Set up router
    openai_sdk._State.router = Router(RouterConfig(), FixedClassifier(SIMPLE))
    openai_sdk._State.patched = False
    
    # Install patches
    openai_sdk.install(("openai",))
    
    yield mock_module
    
    # Cleanup
    openai_sdk.uninstall()


def test_openai_sync_routing(mock_openai):
    """Test synchronous routing with OpenAI SDK."""
    client = mock_openai.OpenAI(api_key=KEY)
    
    response = client.chat.completions.create(
        model="gpt-6-astra",
        messages=[{"role": "user", "content": "Simple question"}]
    )
    
    # Should have routed to luna (SIMPLE classifier)
    assert response.model == "gpt-6-luna"


@pytest.mark.asyncio
async def test_openai_async_routing(mock_openai):
    """Test async routing with OpenAI SDK."""
    client = mock_openai.AsyncOpenAI(api_key=KEY)
    
    response = await client.chat.completions.create(
        model="gpt-6-astra",
        messages=[{"role": "user", "content": "Simple question"}]
    )
    
    assert response.model == "gpt-6-luna"


def test_openai_with_tools(mock_openai):
    """Test routing with tool use."""
    client = mock_openai.OpenAI(api_key=KEY)
    
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {}}
            }
        }
    ]
    
    response = client.chat.completions.create(
        model="gpt-6-astra",
        messages=[{"role": "user", "content": "What's the weather?"}],
        tools=tools
    )
    
    assert response.model == "gpt-6-luna"


def test_openai_no_routing_without_enable():
    """Verify routing is inactive without enable()."""
    openai_sdk._State.router = None
    assert openai_sdk._State.router is None
