"""Provider-direct AI completion layer.

Routing rule (decided by which key is configured in admin AI Settings / env):
- Emergent universal key (admin provider 'emergent' or EMERGENT_LLM_KEY env)
  -> emergentintegrations LlmChat (works on the Emergent platform).
- Admin's OWN OpenAI / Gemini / Claude key -> direct HTTPS call to the
  provider's official API. No Emergent dependency, works on any VPS.
"""
import base64
import logging
import os

import httpx

from ai_key import resolve, resolve_full

log = logging.getLogger("aichat")

# Real provider-side model names for own keys. Emergent catalog names
# (gpt-5.4-mini, gemini-3-flash-preview) do not exist on the public APIs.
OWN_DEFAULT_MODEL = {
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-3.8-flash",
    "anthropic": "claude-haiku-4-5-20251001",
}

_TIMEOUT = httpx.Timeout(2400.0, connect=20.0)  # long read: big files can take tens of minutes


def _pdf_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _fail(provider: str, resp: httpx.Response):
    raise RuntimeError(f"{provider} API error {resp.status_code}: {resp.text[:300]}")


async def _openai(key, model, system, text, files, max_tokens):
    content = [{"type": "text", "text": text}]
    for p in files or []:
        content.append({"type": "input_file",
                        "file_data": f"data:application/pdf;base64,{_pdf_b64(p)}"})
    body = {"model": model, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}]}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions",
                         headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code != 200:
        _fail("OpenAI", r)
    return r.json()["choices"][0]["message"]["content"]


async def _gemini(key, model, system, text, files, max_tokens):
    parts = [{"text": text}]
    for p in files or []:
        parts.append({"inline_data": {"mime_type": "application/pdf", "data": _pdf_b64(p)}})
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": max_tokens,
                                 "thinkingConfig": {"thinkingLevel": "low"}}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(url, headers={"x-goog-api-key": key}, json=body)
    if r.status_code != 200:
        _fail("Gemini", r)
    d = r.json()
    cands = d.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini returned no candidates (finishReason={d.get('promptFeedback')})")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text_out = "".join(pt.get("text", "") for pt in parts)
    if not text_out and cands[0].get("finishReason") == "MAX_TOKENS":
        raise RuntimeError("Gemini hit MAX_TOKENS before emitting text; increase max_tokens.")
    return text_out


async def _anthropic(key, model, system, text, files, max_tokens):
    blocks = [{"type": "document",
               "source": {"type": "base64", "media_type": "application/pdf", "data": _pdf_b64(p)}}
              for p in files or []]
    blocks.append({"type": "text", "text": text})
    body = {"model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": blocks}]}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                         json=body)
    if r.status_code != 200:
        _fail("Claude", r)
    return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")


async def _emergent(key, prov, model, system, text, files, max_tokens):
    from emergentintegrations.llm.chat import LlmChat, UserMessage
    chat = LlmChat(api_key=key, session_id=f"ai-{os.urandom(4).hex()}",
                   system_message=system).with_model(prov, model)
    if files:
        from emergentintegrations.llm.chat import FileContentWithMimeType, TextDelta
        msg = UserMessage(text=text, file_contents=[
            FileContentWithMimeType(file_path=p, mime_type="application/pdf") for p in files])
        parts = []
        async for ev in chat.with_params(max_tokens=max_tokens).stream_message(msg):
            if isinstance(ev, TextDelta):
                parts.append(ev.content)
        return "".join(parts)
    resp = await chat.send_message(UserMessage(text=text))
    return resp if isinstance(resp, str) else str(resp)


async def ai_complete(system: str, text: str, file_paths=None, max_tokens: int = 8192) -> str:
    """One call that works everywhere. Raises RuntimeError on failure."""
    cfg = resolve_full()
    key = cfg["key"]
    if not key:
        raise RuntimeError("No AI key configured (admin AI Settings or EMERGENT_LLM_KEY)")
    prov, model = cfg["provider"], cfg["model"]
    if cfg["emergent"]:
        return await _emergent(key, prov, model, system, text, file_paths, max_tokens)
    # own key: fall back to the provider's real default model if the admin left
    # an Emergent-catalog name in place
    if model in ("gpt-5.4-mini", "gemini-3-flash-preview"):
        model = OWN_DEFAULT_MODEL.get(prov, model)
    if prov == "openai":
        return await _openai(key, model, system, text, file_paths, max_tokens)
    if prov == "gemini":
        return await _gemini(key, model, system, text, file_paths, max_tokens)
    if prov == "anthropic":
        return await _anthropic(key, model, system, text, file_paths, max_tokens)
    raise RuntimeError(f"Unknown AI provider: {prov}")
