"""LiteLLM proxy client and the extraction prompt (PLAN §11).

The system prompt is kept byte-stable so the proxy's prompt cache keeps hitting; do not reflow it.
"""
from __future__ import annotations

import re
import time
from typing import Any

import httpx
import orjson
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import RELEVANCE_PATTERNS, settings
from app.extensions import log
from app.services.cache import bump_stat

SYSTEM_PROMPT = """You extract structured battlefield facts from news items about the war in Ukraine.
For EACH input item return one JSON object. Respond with {"items": [...]} only, no prose.
Per item fields:
- "idx": the input index (integer, echo it back)
- "relevant": bool — is this about a concrete military event/claim in or near Ukraine?
- "event_type": one of "frontline_advance","frontline_claim","deep_strike","shelling",
  "geolocation_proof","debunk","other"
   * frontline_advance: a side reportedly took/entered/liberated a specific settlement
   * frontline_claim: fighting for / assault on a named settlement without a control change
   * deep_strike: drone/missile strike far from the frontline (rear areas, cities, refineries)
   * geolocation_proof: the item itself presents verified imagery/coordinates proving a position
   * debunk: the item argues that earlier footage/geolocation was fake, staged or AI-generated
- "locations": array of {"name": string or null, "lat": number or null, "lon": number or null,
   "oblast": string or null} — extract EVERY named settlement; parse coordinates if literally
   present in the text; NEVER invent coordinates for a name.
   * "oblast": the Ukrainian oblast the place is in, exactly one of: "donetsk","luhansk",
     "zaporizhzhia","kherson","kharkiv","dnipropetrovsk","sumy","chernihiv","mykolaiv","odesa",
     "kyiv","poltava","kirovohrad","cherkasy","vinnytsia","zhytomyr","rivne","volyn","lviv",
     "ternopil","khmelnytskyi","chernivtsi","ivano-frankivsk","zakarpattia","crimea",
     "sevastopol" — or null when unsure or the place is outside Ukraine. Use the article's
     context (direction names, nearby settlements) to fill it in whenever possible.
- "claimed_by": "ru","ua" or null — which side the reported gain/position favors
- "debunk_target": string or null — for debunk items: the settlement/claim/URL being disputed
- "confidence": 0..1 — your confidence in the extraction (not in the claim's truth)"""

TRANSLATION_PROMPT = """You translate news items about the war in Ukraine into English.
Respond with {"items": [...]} only, no prose.
Per item fields:
- "idx": the input index (integer, echo it back)
- "title": the title translated into English
- "body": the body translated into English
Rules:
- Translate faithfully. Do not summarise, editorialise, add or omit facts.
- Render place names in their standard English transliteration (Київ -> Kyiv, Покровськ -> Pokrovsk).
- Keep military abbreviations and unit designations as they are.
- If an item is already in English, echo it back unchanged."""

SUMMARY_PROMPT = """You summarise news items about the war in Ukraine for an English-language map UI.
Respond with {"items": [...]} only, no prose.
Per input item return one object:
- "idx": the input index (integer, echo it back)
- "summary": a 3-5 sentence general English summary of what the item reports
- "locations": one entry per name in the item's input "locations" array — empty array when the
  input array is empty: {"name": the location name echoed back EXACTLY as given,
  "summary": 2-4 English sentences on what the item reports as happening at that specific place}
Rules:
- Always write English, whatever the language of the input.
- Summarise only what the text itself says; never add outside knowledge, context or speculation.
- A location summary is about that place alone; keep item-wide context in the general "summary".
- Never invent, merge or drop locations: echo back exactly the names given, no more, no fewer."""

_RELEVANCE_RE = re.compile("|".join(RELEVANCE_PATTERNS), re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _record_usage(usage: dict | None) -> None:
    """Token accounting for the admin dashboard and for capacity planning (PLAN §18)."""
    if not isinstance(usage, dict):
        return
    for field, stat in (
        ("prompt_tokens", "llm_prompt_tokens"),
        ("completion_tokens", "llm_completion_tokens"),
        ("total_tokens", "llm_total_tokens"),
    ):
        value = usage.get(field)
        if isinstance(value, int) and value > 0:
            bump_stat(stat, value)


class LLMError(RuntimeError):
    pass


class LLMTransient(LLMError):
    """429 / 5xx / network — worth retrying."""


def is_war_relevant(title: str | None, body: str | None) -> bool:
    """Cheap prefilter: no LLM tokens are spent on items that fail it (PLAN §11)."""
    blob = f"{title or ''} {body or ''}"
    if not blob.strip():
        return False
    return bool(_RELEVANCE_RE.search(blob))


# Conservative characters-per-token ratio. Ukrainian/Russian Cyrillic tokenises at roughly
# 2 chars/token on these models (Latin text is nearer 4), so 2.0 keeps the estimate on the safe
# side for the mixed-language corpus this system ingests.
CHARS_PER_TOKEN = 2.0
_LIMITS_CACHE: dict[str, int] = {}


def estimate_tokens(text_body: str | None) -> int:
    if not text_body:
        return 0
    return int(len(text_body) / CHARS_PER_TOKEN) + 1


def context_window() -> int:
    """Model context window, from the proxy's /models when it reports one.

    Cached per process: the value cannot change without a proxy restart, and a failed lookup must
    not add latency to every batch.
    """
    model = settings.litellm_model
    if model in _LIMITS_CACHE:
        return _LIMITS_CACHE[model]

    limit = settings.llm_context_tokens
    base = settings.litellm_api_base.rstrip("/")
    if base:
        try:
            with _client() as client:
                resp = client.get(f"{base}/models")
            if resp.status_code == 200:
                for entry in resp.json().get("data", []):
                    # LiteLLM reports the bare name; settings may carry a provider prefix.
                    if entry.get("id") and model.endswith(entry["id"]):
                        reported = entry.get("max_input_tokens") or entry.get("max_tokens")
                        if reported:
                            limit = min(int(reported), settings.llm_context_tokens)
                        break
        except Exception as exc:
            log.info("could not read model limits from the proxy: %s", exc)

    _LIMITS_CACHE[model] = limit
    log.info("llm context window for %s: %d tokens", model, limit)
    return limit


def fit_batch(items: list[dict], system_prompt: str | None = None) -> list[dict]:
    """Trim a candidate batch to what actually fits the context window.

    Input and output share the window, so the usable input budget is the window minus the system
    prompt minus the reserved output budget. At least one item is always returned — its body is
    truncated as hard as necessary — so a single oversized item can never wedge the queue.
    """
    if not items:
        return []

    budget = (
        context_window()
        - estimate_tokens(system_prompt or SYSTEM_PROMPT)
        - settings.llm_max_tokens
        - 128  # JSON scaffolding, role tokens, and the estimator's own error margin
    )
    if budget < 256:
        log.warning(
            "context window %d leaves only %d input tokens after reserving %d for output; "
            "lower LLM_MAX_TOKENS",
            context_window(), budget, settings.llm_max_tokens,
        )
        budget = 256

    fitted: list[dict] = []
    used = 0
    for item in items:
        cost = estimate_tokens(orjson.dumps(item).decode())
        if fitted and used + cost > budget:
            break
        if not fitted and cost > budget:
            # Single oversized item: shrink its body to fit rather than failing forever.
            overshoot = cost - budget
            keep = max(200, len(item.get("body") or "") - int(overshoot * CHARS_PER_TOKEN) - 200)
            item = {**item, "body": (item.get("body") or "")[:keep] + " …"}
            cost = estimate_tokens(orjson.dumps(item).decode())
        fitted.append(item)
        used += cost
    return fitted


def needs_translation(title: str | None, body: str | None, source_language: str | None) -> bool:
    """Whether an item should be sent to the translator.

    Cyrillic text always needs it. Otherwise the source's catalogued language decides, because
    Latin script alone does not mean English — the catalogue carries Polish, Czech and Romanian
    outlets too. With no language recorded and no Cyrillic, the item is assumed English and left
    alone rather than spending tokens on a no-op translation.
    """
    blob = f"{title or ''} {body or ''}"
    if not blob.strip():
        return False
    if _CYRILLIC_RE.search(blob):
        return True
    language = (source_language or "").strip().lower()
    if not language:
        return False
    return "english" not in language


def _indexed_results(data: dict) -> dict[int, dict]:
    """Re-key an {"items": [...]} completion by the echoed input index."""
    results = data.get("items")
    if not isinstance(results, list):
        raise LLMError(f"missing 'items' array: {str(data)[:200]}")
    out: dict[int, dict] = {}
    for entry in results:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("idx"))
        except (TypeError, ValueError):
            continue
        out[idx] = entry
    return out


def translate_batch(items: list[dict]) -> dict[int, dict]:
    """Translate a batch. items: [{idx, title, body}] -> {idx: {"title":..., "body":...}}."""
    if not items:
        return {}
    data = chat_json(
        [
            {"role": "system", "content": TRANSLATION_PROMPT},
            {"role": "user", "content": orjson.dumps(items).decode()},
        ]
    )
    return _indexed_results(data)


def summarize_batch(items: list[dict]) -> dict[int, dict]:
    """Summarise a batch. items: [{idx, title, body, locations: [name, ...]}] ->
    {idx: {"summary": ..., "locations": [{"name":..., "summary":...}, ...]}}.

    Location summaries are requested once per distinct place *name*; the caller fans them out to
    every map marker sharing that name, so an item with many co-located events costs one entry.
    """
    if not items:
        return {}
    data = chat_json(
        [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": orjson.dumps(items).decode()},
        ]
    )
    return _indexed_results(data)


def fit_batch_for(items: list[dict], system_prompt: str) -> list[dict]:
    """Context-aware batch sizing against an arbitrary system prompt."""
    return fit_batch(items, system_prompt=system_prompt)


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(settings.llm_timeout_s, connect=15.0),
        headers={
            "Authorization": f"Bearer {settings.litellm_api_key}",
            "Content-Type": "application/json",
            "User-Agent": settings.http_user_agent,
        },
    )


def _post(payload: dict) -> dict:
    base = settings.litellm_api_base.rstrip("/")
    if not base or not settings.litellm_model:
        raise LLMError("LiteLLM proxy is not configured (LITELLM_API_BASE/LITELLM_MODEL)")
    url = f"{base}/chat/completions"
    started = time.monotonic()
    with _client() as client:
        resp = client.post(url, content=orjson.dumps(payload))
    bump_stat("llm_latency_ms_total", int((time.monotonic() - started) * 1000))
    if resp.status_code == 429:
        bump_stat("llm_rate_limited")
        raise LLMTransient(f"proxy 429: {resp.text[:200]}")
    if resp.status_code >= 500:
        bump_stat("llm_5xx")
        raise LLMTransient(f"proxy {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        bump_stat("llm_4xx")
        raise LLMError(f"proxy {resp.status_code}: {resp.text[:200]}")
    return resp.json()


@retry(
    retry=retry_if_exception_type((LLMTransient, httpx.TransportError)),
    stop=stop_after_attempt(settings.llm_max_retries),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def chat_json(
    messages: list[dict],
    *,
    allow_response_format: bool = True,
    allow_reasoning_control: bool = True,
) -> dict:
    """One chat-completion call returning parsed JSON.

    Both optional parameters degrade gracefully: a proxy that rejects ``response_format`` falls
    back to prompt-enforced JSON, and one that rejects the reasoning switch falls back to the
    model's default behaviour.
    """
    payload: dict[str, Any] = {
        "model": settings.litellm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": settings.llm_max_tokens,
    }
    if allow_response_format:
        payload["response_format"] = {"type": "json_object"}
    if allow_reasoning_control and settings.llm_reasoning == "none":
        # `reasoning_effort` is the OpenAI-compatible spelling; `chat_template_kwargs` is what
        # vLLM-hosted Qwen builds understand. Both are accepted by LiteLLM, and sending both
        # covers either backend.
        payload["reasoning_effort"] = "none"
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        data = _post(payload)
    except LLMError as exc:
        message = str(exc)
        if allow_reasoning_control and (
            "reasoning_effort" in message or "enable_thinking" in message
            or "chat_template_kwargs" in message
        ):
            log.info("proxy rejected the reasoning switch; retrying without it")
            return chat_json(
                messages,
                allow_response_format=allow_response_format,
                allow_reasoning_control=False,
            )
        if allow_response_format and "response_format" in message:
            log.info("proxy rejected response_format; retrying prompt-enforced")
            return chat_json(
                messages,
                allow_response_format=False,
                allow_reasoning_control=allow_reasoning_control,
            )
        raise
    bump_stat("llm_calls")
    _record_usage(data.get("usage"))
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"unexpected proxy response shape: {str(data)[:200]}") from exc

    content = message.get("content") or ""
    if not content.strip():
        # Reasoning models put their chain of thought in a separate channel. If the answer
        # channel is empty the JSON is usually still recoverable from the reasoning text.
        content = message.get("reasoning_content") or ""
        if content.strip():
            log.info("recovering JSON from the reasoning channel (empty content)")
    if not content.strip():
        raise LLMError(
            f"empty completion (finish_reason={choice.get('finish_reason')}, "
            f"usage={data.get('usage')}) — raise LLM_MAX_TOKENS if it is 'length'"
        )
    if choice.get("finish_reason") == "length":
        log.warning("completion hit the token ceiling; JSON may be truncated")
    return parse_json_loose(content)


def parse_json_loose(content: str) -> dict:
    """Parse model output defensively: strip fences/prose, then take the outermost JSON object."""
    if not content:
        raise LLMError("empty completion")
    text_body = content.strip()
    if text_body.startswith("```"):
        text_body = re.sub(r"^```(?:json)?|```$", "", text_body, flags=re.MULTILINE).strip()
    try:
        return orjson.loads(text_body)
    except orjson.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(text_body)
    if not match:
        raise LLMError(f"no JSON object in completion: {text_body[:200]}")
    try:
        return orjson.loads(match.group(0))
    except orjson.JSONDecodeError as exc:
        raise LLMError(f"unparseable JSON: {text_body[:200]}") from exc


def build_messages(items: list[dict]) -> list[dict]:
    """items: [{idx, source_name, published_at, title, body}]"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": orjson.dumps(items).decode()},
    ]


def extract_batch(items: list[dict]) -> dict[int, dict]:
    """Run one extraction batch and return ``{idx: item_result}``."""
    if not items:
        return {}
    return _indexed_results(chat_json(build_messages(items)))


def truncate(text_body: str | None) -> str:
    if not text_body:
        return ""
    limit = settings.llm_max_body_chars
    return text_body if len(text_body) <= limit else text_body[:limit] + " …"
