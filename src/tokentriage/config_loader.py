"""Load tokentriage configuration from files or environment."""

from __future__ import annotations

import logging
import os
from dataclasses import fields
from pathlib import Path
from typing import Any

from .config import TIERS, RouterConfig
from .providers import PROVIDERS

log = logging.getLogger("tokentriage")


def find_config_file(start_dir: Path | None = None, max_depth: int = 5) -> Path | None:
    """Find tokentriage config file in current or parent directories."""
    start_dir = start_dir or Path.cwd()
    current = start_dir
    
    for _ in range(max_depth):
        for name in ["tokentriage.yaml", "tokentriage.yml", "tokentriage.toml", ".tokentriage"]:
            path = current / name
            if path.exists():
                return path
        
        # Also check for [tool.tokentriage] in pyproject.toml
        pyproject = current / "pyproject.toml"
        if pyproject.exists():
            try:
                import tomllib
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
                    if data.get("tool", {}).get("tokentriage"):
                        return pyproject
            except Exception:
                pass
        
        if current.parent == current:
            break
        current = current.parent
    
    return None


def load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML config file."""
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        raise ImportError("pyyaml not installed; install with: pip install pyyaml")


def load_toml(path: Path) -> dict[str, Any]:
    """Load TOML config file."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            raise ImportError("tomli not installed for Python < 3.11; install with: pip install tomli")
    
    with open(path, "rb") as f:
        data = tomllib.load(f)
        if path.name == "pyproject.toml":
            return data.get("tool", {}).get("tokentriage", {})
        return data


def load_config_file(path: Path) -> dict[str, Any]:
    """Load config from file, auto-detect format."""
    if path.suffix in {".yaml", ".yml"} or path.name == ".tokentriage":
        return load_yaml(path)
    elif path.suffix == ".toml" or path.name == "pyproject.toml":
        return load_toml(path)
    else:
        raise ValueError(f"Unknown config format: {path.name}")


def load_config_from_env(prefix: str = "TOKENTRIAGE_") -> dict[str, Any]:
    """Load config from environment variables."""
    config: dict[str, Any] = {}
    
    # Simple env vars
    if os.getenv(f"{prefix}ENABLED"):
        config["enabled"] = os.getenv(f"{prefix}ENABLED").lower() == "true"
    
    backend = os.getenv(f"{prefix}BACKEND")
    if backend:
        config["backend"] = backend
    
    timeout = os.getenv(f"{prefix}TIMEOUT_S")
    if timeout:
        config["timeout_s"] = float(timeout)
    
    log_level = os.getenv(f"{prefix}LOG_LEVEL")
    if log_level:
        config["log_level"] = log_level
    
    # Frameworks list
    frameworks = os.getenv(f"{prefix}FRAMEWORKS")
    if frameworks:
        config["frameworks"] = [f.strip() for f in frameworks.split(",")]
    
    # Providers list
    providers = os.getenv(f"{prefix}PROVIDERS")
    if providers:
        config["providers"] = [p.strip() for p in providers.split(",")]
    
    return config


def _trigger_lev_download(checkpoint: str = "interfaze-ai/lev") -> bool:
    """Make sure lev is downloaded (blocking, with progress), then load and smoke-test it.

    LEV is a 4B decision model from InterfazeAI that classifies LLM requests
    into complexity tiers (simple/standard/complex). It is a ~0.2 GB adapter on a
    ~9.3 GB base model, cached in ~/.cache/huggingface/hub/.

    Returns: True if successful, False if fallback to heuristic needed.
    """
    try:
        import lev

        from .lev_download import ensure_lev_cached

        if not ensure_lev_cached(checkpoint):
            return False

        import time
        print("🔄 tokentriage: loading LEV into memory from the local cache...")
        start = time.perf_counter()
        model = lev.load(checkpoint)
        load_time = time.perf_counter() - start

        print(f"✅ tokentriage: LEV loaded into memory in {load_time:.1f}s")
        print()

        # Test the model with a sample request to verify it works
        print("🧪 Testing LEV classification...")
        test_questions = {
            "tier": {
                "type": "choice",
                "instructions": "How capable a language model does this request need?",
                "criteria": {
                    "simple": "short factual answer, lookup, greeting",
                    "standard": "multi-paragraph writing, summarizing",
                    "complex": "multi-step reasoning, system design",
                },
            }
        }

        # Test with a simple request
        result = model.system_one("What is 2+2?", test_questions)
        tier_probs = result.answers["tier"].probabilities

        print(f"   Sample: 'What is 2+2?'")
        print(f"   → Simple: {tier_probs['simple']:.1%}")
        print(f"   → Standard: {tier_probs['standard']:.1%}")
        print(f"   → Complex: {tier_probs['complex']:.1%}")
        print()

        print("✅ tokentriage: LEV model ready for routing!")
        print("   Requests will now be routed based on complexity analysis")
        print()

        # Hand the loaded engine to the classifier so warmup() does not load a second ~9 GB copy.
        from .classifiers.lev_local import LevLocalClassifier
        LevLocalClassifier._engine = model

        return True

    except ImportError:
        print("⚠️  tokentriage: lev package not installed")
        print("   LEV is a separate prerequisite (not on PyPI)")
        print()
        print("   To install LEV:")
        print("   pip install 'lev[serve] @ git+https://github.com/InterfazeAI/lev#subdirectory=packages/lev'")
        print()
        print("   Or use the heuristic backend for now (75% accurate, instant)")
        print()
        return False

    except Exception as e:
        print(f"⚠️  tokentriage: LEV download/test failed: {e}")
        print("   Falling back to heuristic routing (75% accurate, <1ms)")
        print()
        return False


def _models_to_tiers(models: dict) -> tuple[dict, dict]:
    """`models: {simple: {providers: ["anthropic/claude-haiku-4-5"]}}` -> (tiers, openrouter_families).

    "provider/model" sets that provider's tier; "openrouter/vendor/model" sets the OpenRouter family
    tier to the OpenRouter id "vendor/model". The first entry per provider and tier wins.
    """
    tiers: dict[str, dict[str, str]] = {}
    families: dict[str, dict[str, str]] = {}
    for tier, spec in models.items():
        entries = spec.get("providers", []) if isinstance(spec, dict) else []
        if tier not in TIERS:
            print(f"⚠️  tokentriage: models.{tier} ignored (tiers are {', '.join(TIERS)})")
            continue
        for entry in entries:
            head, _, rest = str(entry).partition("/")
            if head == "openrouter" and "/" in rest:
                families.setdefault(rest.split("/", 1)[0], {}).setdefault(tier, rest)
            elif head in PROVIDERS and rest:
                tiers.setdefault(head, {}).setdefault(tier, rest)
            else:
                print(f"⚠️  tokentriage: models.{tier} entry {entry!r} ignored; use provider/model "
                      f"({', '.join(PROVIDERS)}) or openrouter/vendor/model")
    return tiers, families


def load_config(config_path: str | None = None, prefix: str = "TOKENTRIAGE_", auto_enable: bool = True) -> tuple[RouterConfig, list[str], list[str] | None]:
    """Load configuration from file, env, or defaults.

    Args:
        config_path: Optional path to config file
        prefix: Environment variable prefix (default: TOKENTRIAGE_)
        auto_enable: If True, automatically enable routing and download LEV model if lev-local

    Returns: (RouterConfig, frameworks_list, providers_list)
    """
    # Start with env vars
    config_dict = load_config_from_env(prefix)

    # Try to find and load config file
    if config_path is None:
        config_path_obj = find_config_file()
    else:
        config_path_obj = Path(config_path)

    if config_path_obj:
        file_config = load_config_file(config_path_obj)
        log.debug("tokentriage: loaded config file %s", config_path_obj)
        # File config takes precedence over env (file is more specific)
        config_dict.update(file_config)

    # Extract frameworks list (separate from RouterConfig)
    frameworks = tuple(config_dict.pop("frameworks", ("langchain",)))
    providers = tuple(config_dict.pop("providers", None)) if config_dict.get("providers") else None

    # Flatten nested 'router' dict into top-level config
    if "router" in config_dict:
        router_config = config_dict.pop("router")
        config_dict.update(router_config)

    enabled = config_dict.pop("enabled", True)
    if "models" in config_dict:
        tiers, families = _models_to_tiers(config_dict.pop("models") or {})
        # An explicit `tiers:` / `openrouter_families:` section wins over `models:`.
        for provider, mapping in (config_dict.get("tiers") or {}).items():
            tiers.setdefault(provider, {}).update(mapping)
        for vendor, mapping in (config_dict.get("openrouter_families") or {}).items():
            families.setdefault(vendor, {}).update(mapping)
        config_dict["tiers"], config_dict["openrouter_families"] = tiers, families

    # Keep only real RouterConfig fields. Anything else (e.g. `models:`, `features:`) is reported,
    # never allowed to make the whole file silently fall back to defaults.
    valid = {f.name for f in fields(RouterConfig)}
    ignored = sorted(k for k in config_dict if k not in valid)
    if ignored:
        print(f"⚠️  tokentriage: ignoring config keys that are not RouterConfig settings: {', '.join(ignored)}")
    config_dict = {k: v for k, v in config_dict.items() if k in valid}
    for key in ("never_route", "openrouter_vendors"):
        if isinstance(config_dict.get(key), list):
            config_dict[key] = tuple(config_dict[key])
    cfg = RouterConfig(**config_dict)
    log.debug("tokentriage: backend=%s timeout_s=%s block_on_load=%s auto_enable=%s",
              cfg.backend, cfg.timeout_s, cfg.block_on_load, auto_enable)

    if not enabled:
        print("tokentriage: disabled in config (enabled: false); calls are not routed")
        return cfg, frameworks, providers

    # If lev-local backend is configured, trigger model download
    if auto_enable and cfg.backend == "lev-local":
        if not _trigger_lev_download(cfg.lev_checkpoint):
            # Fallback to heuristic if download fails
            print("   Switching to heuristic backend for now")
            cfg.backend = "heuristic"

    # Warmup LEV BEFORE enabling hooks if block_on_load is set
    if cfg.backend == "lev-local" and cfg.block_on_load:
        try:
            from .classifiers.lev_local import LevLocalClassifier
            print("⏳ tokentriage: Preloading LEV model (this may take 5-10 seconds)...")
            classifier = LevLocalClassifier(cfg.lev_checkpoint, cfg.timeout_s, cfg.block_on_load)
            classifier.warmup()
            print("✅ tokentriage: LEV model preloaded and ready")
        except Exception as e:
            print(f"⚠️  tokentriage: LEV preload failed: {e}")

    # Auto-enable routing if requested
    if auto_enable:
        from . import enable
        try:
            kwargs = {"frameworks": frameworks}
            if providers:
                kwargs["providers"] = providers
            installed = enable(cfg, **kwargs)
            if installed:
                print(f"✅ tokentriage: Routing enabled for {', '.join(installed)}")
        except Exception as e:
            print(f"⚠️  tokentriage: Failed to enable routing: {e}")

    return cfg, frameworks, providers
