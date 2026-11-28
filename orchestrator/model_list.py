"""The models a provider offers, as suggestions for the page's model fields (GET /api/models): asked of the
provider itself, with the orchestrator's keys (Settings), so the list is never out of date and the browser never
holds a key. A suggestion only: any model id can still be typed.

- anthropic: GET /v1/models (its API, or base_url: an Anthropic-compatible endpoint).
- gemini: GET /v1beta/models, those that generate content.
- openai: GET <base_url>/models; without a base URL, OpenAI's own, its chat models (not its embedding, speech or
  image ones).
- llamacpp: GET <base_url>/v1/models (llama-server's, the model it serves).
- ollama: GET <host>/api/tags.

Anything else (bedrock, a plugin) has no list: {"models": [], "why": ...}. So has a provider that cannot be reached
or refuses: why says so, and the field is left for typing. Each answer is kept for CACHE_SECONDS."""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from harness import credentials

ANTHROPIC = "https://api.anthropic.com"
GEMINI = "https://generativelanguage.googleapis.com"
OPENAI = "https://api.openai.com/v1"
OLLAMA = "http://localhost:11434"
CACHE_SECONDS = 600
TIMEOUT = 8
LIMIT = 4 << 20  # bytes of a reply read: OpenAI's list is long, not this long
# OpenAI's own models that are not for chat: left out of its list (a compatible server's is given whole).
NOT_CHAT = re.compile(r"embedding|tts|whisper|dall-e|moderation|transcribe|realtime|audio|image|search|davinci|babbage"
                      r"|sora", re.I)

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


class Refused(Exception):
    """A provider that could not be asked: the reason, for the page."""


def _get(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read(LIMIT))
    except urllib.error.HTTPError as err:
        raise Refused(f"{urllib.parse.urlsplit(url).netloc} answered {err.code}"
                      + (" (is the key right?)" if err.code in (401, 403) else "")) from err
    except (OSError, ValueError) as err:
        raise Refused(f"could not ask {urllib.parse.urlsplit(url).netloc}: {type(err).__name__}") from err


def _ids(found: Any, key: str = "data", field: str = "id") -> list[str]:
    items = found.get(key) if isinstance(found, dict) else None
    return [str(item[field]) for item in items or [] if isinstance(item, dict) and item.get(field)]


def _named(ids: list[str]) -> list[str]:
    """A server's ids, a model file's path (llama-server without --alias lists /models/X/X.gguf) as its name, X: the
    server answers to any id, and that is the one a person knows it by."""
    return [re.sub(r"\.gguf$", "", name.rsplit("/", 1)[-1]) if name.endswith(".gguf") else name for name in ids]


def _anthropic(base_url: str) -> list[str]:
    key = credentials.get("ANTHROPIC_API_KEY")
    if not key:
        raise Refused("no ANTHROPIC_API_KEY in Settings (a Claude token cannot list models)")
    return _ids(_get(f"{(base_url or ANTHROPIC).rstrip('/')}/v1/models?limit=1000",
                     {"x-api-key": key, "anthropic-version": "2023-06-01"}))


def _gemini(_: str) -> list[str]:
    key = credentials.first(("GOOGLE_API_KEY", "GEMINI_API_KEY"))
    if not key:
        raise Refused("no GOOGLE_API_KEY in Settings")
    found, page = [], ""
    for _ in range(10):
        reply = _get(f"{GEMINI}/v1beta/models?pageSize=1000" + (f"&pageToken={urllib.parse.quote(page)}" if page else ""),
                     {"x-goog-api-key": key})
        for item in reply.get("models") or [] if isinstance(reply, dict) else []:
            if isinstance(item, dict) and "generateContent" in (item.get("supportedGenerationMethods") or []):
                found.append(str(item.get("name", "")).removeprefix("models/"))
        if not (page := reply.get("nextPageToken") if isinstance(reply, dict) else ""):
            break
    return [name for name in found if name]


def _openai(base_url: str) -> list[str]:
    key = credentials.get("OPENAI_API_KEY")
    if not base_url and not key:
        raise Refused("no OPENAI_API_KEY in Settings")
    ids = _ids(_get(f"{(base_url or OPENAI).rstrip('/')}/models", {"Authorization": f"Bearer {key}"} if key else {}))
    return _named(ids) if base_url else [name for name in ids if not NOT_CHAT.search(name)]


def _llamacpp(base_url: str) -> list[str]:
    if not base_url:
        raise Refused("give the server's base URL")
    return _named(_ids(_get(f"{re.sub(r'/v1/?$', '', base_url.rstrip('/'))}/v1/models")))


def _ollama(base_url: str) -> list[str]:
    return _ids(_get(f"{(base_url or OLLAMA).rstrip('/')}/api/tags"), "models", "name")


LISTS = {"anthropic": _anthropic, "gemini": _gemini, "openai": _openai, "llamacpp": _llamacpp, "ollama": _ollama}


def models(provider: str, base_url: str = "") -> dict[str, Any]:
    """{"models": [ids, sorted], "why": "" or why there are none}, kept for CACHE_SECONDS (a failure too, briefly)."""
    lister = LISTS.get(provider)
    if lister is None:
        return {"models": [], "why": f"no list of models for provider {provider or 'default'}: type its id"}
    key, now = (provider, base_url), time.monotonic()
    with _lock:
        if (kept := _cache.get(key)) and kept[0] > now:
            return kept[1]
    try:
        answer = {"models": sorted(set(lister(base_url))), "why": ""}
        if not answer["models"]:
            answer["why"] = "the provider listed no models"
        keep = CACHE_SECONDS
    except Refused as err:
        answer, keep = {"models": [], "why": str(err)}, 30
    with _lock:
        _cache[key] = (now + keep, answer)
    return answer
