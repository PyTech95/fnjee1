"""Admin-configurable AI key resolution.

Priority: key saved by admin in DB (db.settings id='ai') > EMERGENT_LLM_KEY env var.
Single uvicorn worker, so a module-level cache is sufficient; refreshed on startup
and after every admin save/reset.
"""
import os
import logging

log = logging.getLogger("aikey")

# admin-facing provider -> (LlmChat provider, default model)
PROVIDERS = {
    "emergent": ("gemini", "gemini-3-flash-preview"),
    "openai": ("openai", "gpt-4.1-mini"),
    "gemini": ("gemini", "gemini-3.8-flash"),
    "claude": ("anthropic", "claude-haiku-4-5-20251001"),
}

_cfg = {"provider": "emergent", "api_key": None, "model": None, "source": "env"}


def _env_key():
    """Self-hosted fallback: read a provider key straight from the environment.
    Lets a VPS configure AI via backend/.env (GEMINI_API_KEY / OPENAI_API_KEY /
    ANTHROPIC_API_KEY) without opening Admin → AI Settings. Returns
    (api_key, provider, model) or None."""
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"], "gemini", PROVIDERS["gemini"][1]
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"], "openai", PROVIDERS["openai"][1]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"], "anthropic", PROVIDERS["claude"][1]
    return None


async def load_from_db(db):
    doc = await db.settings.find_one({"id": "ai"}, {"_id": 0})
    if doc and doc.get("api_key"):
        _cfg.update(provider=doc.get("provider", "emergent"), api_key=doc["api_key"],
                    model=doc.get("model"), source="admin")
    else:
        env = _env_key()
        if env:
            _cfg.update(provider=env[1], api_key=None, model=None, source="env-key")
        else:
            _cfg.update(provider="emergent", api_key=None, model=None, source="env")


def resolve():
    """Returns (api_key, llmchat_provider, model).
    Priority: admin key saved in DB > provider key in env (GEMINI/OPENAI/ANTHROPIC)
    > EMERGENT_LLM_KEY env var."""
    if _cfg.get("api_key"):
        prov, def_model = PROVIDERS.get(_cfg["provider"], PROVIDERS["emergent"])
        return _cfg["api_key"], prov, _cfg.get("model") or def_model
    env = _env_key()
    if env:
        return env  # (key, provider, model)
    return os.environ.get("EMERGENT_LLM_KEY", ""), "gemini", "gemini-3.8-flash"


def resolve_full():
    key, prov, model = resolve()
    emergent = (_cfg["source"] == "env" and not _env_key()) or key.startswith("sk-emergent-")
    return {"key": key, "provider": prov, "model": model, "emergent": emergent}


def status():
    key, prov, model = resolve()
    default_models = {k: v[1] for k, v in PROVIDERS.items()}
    return {
        "provider": _cfg["provider"] if _cfg.get("api_key") else prov,
        "model": _cfg.get("model") or default_models.get(prov),
        "source": _cfg["source"],
        "masked_key": mask_key(key),
        "env_key_present": bool(os.environ.get("EMERGENT_LLM_KEY") or _env_key()),
        "effective_model": model,
        "providers": list(PROVIDERS.keys()),
        "default_models": default_models,
    }


def mask_key(key):
    if not key:
        return None
    if len(key) <= 8:
        return key[:2] + "…"
    return key[:4] + "…" + key[-4:]
