import os
import time
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.core.config import (
    COFORGE_API_KEY,
    COFORGE_API_URL,
    COFORGE_CPT_API_KEY,
    COFORGE_CPT_API_URL,
    COFORGE_CPT_MODEL,
    COFORGE_MODEL,
    PROJECT_ROOT,
)


# The Coforge LLM Router intermittently answers valid requests with HTTP 400
# "An unexpected error occurred while generating the response." (~40-50% of
# calls in testing). Only that specific error is retried; real 400s fail fast.
TRANSIENT_ROUTER_ATTEMPTS = 5
TRANSIENT_ROUTER_MESSAGE = "unexpected error occurred while generating the response"


def _is_transient_router_error(response):
    return response.status_code == 400 and TRANSIENT_ROUTER_MESSAGE in response.text.lower()


def _call_llm_with_config(prompt, system_prompt=None, temperature=0.3, api_key=None, api_url=None, model=None, key_name="COFORGE_API_KEY"):
    """Call the Coforge LLM Router API using the supplied settings."""

    clean_api_key = (api_key or "").strip()
    clean_api_url = (api_url or "").strip().replace('"', '').replace("%22", "")
    clean_model = (model or "").strip()

    if not clean_api_key:
        raise ValueError(f"{key_name} not found in .env file.")

    if not clean_api_url:
        raise ValueError(f"{key_name.replace('_API_KEY', '_API_URL')} not found in .env file.")

    if not clean_model:
        raise ValueError(
            f"{key_name.replace('_API_KEY', '_MODEL')} is empty. Set it in .env to the exact model name enabled for your Coforge AI Studio account."
        )

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": clean_api_key,
    }

    messages = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": prompt})

    body = {
        "model": clean_model,
        "messages": messages,
        "temperature": temperature,
    }

    retry_policy = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["POST"],
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry_policy))
    session.mount("http://", HTTPAdapter(max_retries=retry_policy))

    for attempt in range(TRANSIENT_ROUTER_ATTEMPTS):
        try:
            response = session.post(
                clean_api_url,
                headers=headers,
                json=body,
                timeout=(15, 120),
            )
        except requests.exceptions.RequestException as error:
            raise ConnectionError(
                "Unable to connect to the configured Coforge LLM service after retries."
            ) from error
        if not _is_transient_router_error(response) or attempt == TRANSIENT_ROUTER_ATTEMPTS - 1:
            break
        time.sleep(1.5 * (attempt + 1))

    if response.status_code != 200:
        raise RuntimeError(
            f"Coforge API error: {response.status_code} - {response.text}"
        )

    data = response.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Grok (xAI) / Groq - OpenAI-compatible chat completions, used when GROK_API_KEY is set.
# .env is re-read on every call, so pasting the key takes effect without a restart.
# ---------------------------------------------------------------------------
GROK_PROVIDERS = {
    # key prefix: (provider name, base URL, preferred models in order)
    "xai-": ("xAI Grok", "https://api.x.ai/v1",
             ["grok-4-fast-non-reasoning", "grok-4-fast", "grok-3-mini", "grok-4", "grok-3"]),
    "gsk_": ("Groq", "https://api.groq.com/openai/v1",
             ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "llama-3.1-70b-versatile", "llama-3.1-8b-instant"]),
}
_NON_CHAT_MODEL_HINTS = ("whisper", "tts", "guard", "image", "vision", "embed", "imagine", "video")


def _grok_settings():
    from dotenv import dotenv_values

    # The .env file is re-read each call; a non-empty real environment variable still wins.
    # (Empty values loaded into os.environ at startup must not mask a key pasted later.)
    values = {**dotenv_values(PROJECT_ROOT / ".env"),
              **{k: v for k, v in os.environ.items() if k.startswith("GROK_") and v.strip()}}
    key = str(values.get("GROK_API_KEY") or "").strip().strip('"').strip("'")
    if not key:
        return None
    name, base_url, preferred = GROK_PROVIDERS.get(key[:4], GROK_PROVIDERS["xai-"])
    base_url = str(values.get("GROK_API_URL") or base_url).strip().rstrip("/")
    model = str(values.get("GROK_MODEL") or "").strip().strip('"')
    return {"name": name, "key": key, "base_url": base_url, "model": model, "preferred": tuple(preferred)}


@lru_cache(maxsize=4)
def _pick_grok_model(base_url, key, preferred):
    """Choose a chat model the key can use (first preferred one available)."""
    response = requests.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if response.status_code != 200:
        raise RuntimeError(f"Grok API rejected the key ({response.status_code}): {response.text[:300]}")
    available = [item.get("id", "") for item in response.json().get("data", [])]
    for model in preferred:
        if model in available:
            return model
    chat = [model for model in available if not any(hint in model.lower() for hint in _NON_CHAT_MODEL_HINTS)]
    if not chat:
        raise RuntimeError("No chat model is available for this Grok API key.")
    return chat[0]


def _call_grok(settings, prompt, system_prompt=None, temperature=0.3):
    model = settings["model"] or _pick_grok_model(settings["base_url"], settings["key"], settings["preferred"])
    messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [
        {"role": "user", "content": prompt}
    ]
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST"], raise_on_status=False,
    )))
    try:
        response = session.post(
            f"{settings['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {settings['key']}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "temperature": temperature},
            timeout=(15, 180),
        )
    except requests.exceptions.RequestException as error:
        raise ConnectionError(f"Unable to connect to {settings['name']} after retries.") from error
    if response.status_code != 200:
        raise RuntimeError(f"{settings['name']} API error: {response.status_code} - {response.text[:500]}")
    return response.json()["choices"][0]["message"]["content"]


def call_llm(prompt, system_prompt=None, temperature=0.3):
    """Call Grok when GROK_API_KEY is set in .env, otherwise the Coforge LLM Router."""
    grok = _grok_settings()
    if grok:
        return _call_grok(grok, prompt, system_prompt, temperature)
    return _call_llm_with_config(
        prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        api_key=COFORGE_API_KEY,
        api_url=COFORGE_API_URL,
        model=COFORGE_MODEL,
        key_name="COFORGE_API_KEY",
    )


def call_llm_for_cpt(prompt, system_prompt=None, temperature=0.3):
    """Call the dedicated CPT generation LLM configuration if provided.

    The CPT override swaps the API key and endpoint, but keeps the standard model
    selection unless no general model is configured.
    """
    grok = _grok_settings()
    if grok:
        return _call_grok(grok, prompt, system_prompt, temperature)
    model = COFORGE_MODEL or COFORGE_CPT_MODEL
    return _call_llm_with_config(
        prompt,
        system_prompt=system_prompt,
        temperature=temperature,
        api_key=COFORGE_CPT_API_KEY,
        api_url=COFORGE_CPT_API_URL,
        model=model,
        key_name="COFORGE_CPT_API_KEY",
    )
