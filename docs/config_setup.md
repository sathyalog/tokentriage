# Config-Based Zero-Code Setup

Enable tokentriage for your entire project with a single configuration file—no code changes needed.

> **New in v0.1.0+:** `load_config()` now does everything automatically:
> - ✅ Validates LEV prerequisites with helpful installation guide
> - ✅ Downloads 9.2GB LEV model (first run only, 5-10 min)
> - ✅ Tests LEV with sample classification
> - ✅ Enables routing for all configured frameworks
> - ✅ Falls back gracefully to heuristic if LEV unavailable
>
> See [README.md#What's New](../README.md#whats-new-in-v010) for details.

## Quick Start

### Step 1: Create `tokentriage.yaml`

```yaml
# In your project root
enabled: true
frameworks:
  - langchain
  - anthropic
  - openai

router:
  backend: lev-local
  timeout_s: 2.0
```

### Step 2: Add One Line to Your Entry Point

```python
# main.py, app.py, __init__.py, or settings.py
import tokentriage
tokentriage.load_config()

# Everything else stays the same
from langchain_anthropic import ChatAnthropic
from anthropic import Anthropic
llm1 = ChatAnthropic(model="claude-opus-5-5")
client = Anthropic()

llm1.invoke("Hi")  # ✨ Automatically routed
client.messages.create(model="claude-opus-5-5", messages=[...])  # ✨ Automatically routed
```

### Step 3: Done!

All LLM calls across your codebase are routed based on complexity.

---

## Configuration Files

### YAML Format (`tokentriage.yaml`)

```yaml
enabled: true

frameworks:
  - langchain
  - anthropic
  - openai

providers:
  - anthropic
  - openai

router:
  backend: lev-local          # or lev-http, heuristic
  timeout_s: 2.0
  min_tier: null              # or standard, complex
  allow_upgrade: true
```

### TOML Format (`pyproject.toml`)

```toml
[tool.tokentriage]
enabled = true
frameworks = ["langchain", "anthropic", "openai"]
providers = ["anthropic", "openai"]

[tool.tokentriage.router]
backend = "lev-local"
timeout_s = 2.0
```

### Environment Variables

```bash
export TOKENTRIAGE_ENABLED=true
export TOKENTRIAGE_FRAMEWORKS=langchain,anthropic,openai
export TOKENTRIAGE_BACKEND=lev-local
export TOKENTRIAGE_TIMEOUT_S=2.0
```

---

## Load Configuration Anywhere

```python
import tokentriage

# Auto-finds tokentriage.yaml or [tool.tokentriage] in pyproject.toml
config, frameworks, providers = tokentriage.load_config()

# Use the config
tokentriage.enable(config=config, frameworks=frameworks, providers=providers)
```

Or let `enable()` handle it:

```python
# Enable will call load_config() internally
tokentriage.enable(frameworks=["langchain", "anthropic", "openai"])
```

---

## Production Deployment

### Docker with Config File

```dockerfile
FROM python:3.12
WORKDIR /app

COPY tokentriage.yaml .
COPY pyproject.toml .
COPY requirements.txt .

RUN pip install -r requirements.txt

COPY . .

CMD ["python", "-m", "myapp"]
```

```python
# myapp/__init__.py
import tokentriage
tokentriage.load_config()  # Reads tokentriage.yaml from root
```

### Kubernetes ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: tokentriage-config
data:
  tokentriage.yaml: |
    enabled: true
    frameworks: [langchain, anthropic]
    router:
      backend: lev-http
      lev_url: http://lev-service:8000
```

```python
# app.py
import tokentriage
tokentriage.load_config()  # Mounts from /etc/config/tokentriage.yaml
```

---

## Configuration Precedence

1. **Command-line config** (if passed to `enable()`)
2. **Config file** (`tokentriage.yaml`, `pyproject.toml`)
3. **Environment variables** (`TOKENTRIAGE_*`)
4. **Defaults** (lev-local backend, LangChain framework)

### Example: Override Backend via Environment

```bash
# Config file says lev-local, but override to heuristic in production
export TOKENTRIAGE_BACKEND=heuristic
python myapp.py
```

---

## Configuration Schema

```yaml
enabled: bool = True                    # Enable routing
frameworks: list[str] = ["langchain"]   # Which SDKs to route (langchain, anthropic, openai)
providers: list[str] = []               # Filter providers (optional, empty = all)

router:
  backend: str = "lev-local"            # lev-local, lev-http, heuristic (default: lev-local)
  lev_checkpoint: str = "interfaze-ai/lev"  # Hugging Face model ID for lev-local
  lev_url: str = "http://localhost:8000"    # lev serve address for lev-http
  lev_api_key: str | None = None        # Bearer token for protected lev serve
  timeout_s: float = 1.5                # Timeout for lev decision (seconds)
  block_on_load: bool = False           # Block on LEV load or route with heuristic
  cache_size: int = 1024                # Decision cache size
  max_state_chars: int = 2000           # Max characters of user message sent to classifier
  
  # Thresholds for complexity classification
  complex_threshold: float = 0.5        # Complex if score reaches this
  simple_threshold: float = 0.55        # Simple if score reaches this
  min_confidence: float = 0.45          # Move up a tier if confidence below this
  
  # Guardrails
  allow_upgrade: bool = True            # Allow routing to pricier model?
  min_tier: str | None = None           # Quality floor (simple/standard/complex)
  never_route: tuple[str, ...] = (...)  # Model patterns that are never swapped
  provider_hosts: dict[str, str] = {}   # Extra endpoint hosts to treat as provider
  capability_guard: bool = True         # Move up tier if model lacks capability
  redact_pii: bool = True               # Redact PII in everything tokentriage writes
  allow_remote_lev: bool = False        # Allow non-local lev_url (lev-http only)
  redact_classifier_input: bool = True  # Redact state sent to lev-http server

log_level: str = "INFO"                 # DEBUG, INFO, WARNING, ERROR
log_format: str = "text"                # text or json
log_prompt_chars: int = 60              # Characters of prompt shown in logs
log_path: str | None = None             # JSONL file for call logs
summary_at_exit: bool = True            # Log summary when process exits

# Usage tracking (last 24h)
usage_file: bool = True                 # Use rolling hourly usage files
usage_live: bool = True                 # Use in-memory live usage socket
usage_retention_hours: int = 24         # How long to keep usage data
usage_memory_max: int = 100000          # Max calls kept in memory per process
```

---

## Migration: Code-Based → Config-Based

### Before (Scattered Throughout Codebase)

```python
# file1.py
import tokentriage
tokentriage.enable(frameworks=["langchain"])

# file2.py
import tokentriage
tokentriage.enable(frameworks=["langchain", "anthropic"])

# file3.py
import tokentriage
tokentriage.enable(frameworks=["langchain"])
# ... repeated 50+ times
```

### After (Single Config File)

```yaml
# tokentriage.yaml
frameworks:
  - langchain
  - anthropic
  - openai
```

```python
# main.py (only place with tokentriage code)
import tokentriage
tokentriage.load_config()
```

All 50+ files now route automatically with zero changes to their code.

---

## Troubleshooting

**Config not found?**
- Check file location: should be in project root or use `TOKENTRIAGE_CONFIG_PATH` env var
- Supported names: `tokentriage.yaml`, `tokentriage.yml`, `pyproject.toml`

**Framework not loaded?**
- Verify SDK is installed: `python -c "import anthropic"` or `import openai`
- Check framework name is correct: lowercase ("anthropic", not "Anthropic")

**Changes not taking effect?**
- `load_config()` must be called BEFORE creating LLM instances
- Restart your app after changing config file
- Set `TOKENTRIAGE_LOG_LEVEL=DEBUG` to see what's loading
