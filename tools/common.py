import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
REPORTS_DIR = TMP / "reports"
EXTRACT_DIR = TMP / "extractions"
OUTPUT_DIR = TMP / "output"
UPLOAD_DIR = TMP / "uploads"

load_dotenv(ROOT / ".env")

MODEL = "claude-opus-5-5"
LOCATOR_MODEL = "claude-haiku-5-5"  # cheap model that only finds pages (page_finder.py)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# USD per million tokens for claude-opus-5-5
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 4.00, 20.00, 0.20, 5.00
# claude-haiku-5-5: $0.10 / $0.50 for prompts up to 100K tokens, $0.50 / $2.50 beyond
HAIKU_PRICES = ((100_000, 0.10, 0.50), (float("inf"), 0.50, 2.50))


def get_client() -> anthropic.Anthropic:
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add a line ANTHROPIC_API_KEY=sk-ant-... to the .env file."
        )
    return anthropic.Anthropic(max_retries=4)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:20]


def usage_cost(usage, model: str = MODEL) -> float:
    if model == LOCATOR_MODEL:
        prompt = (usage.input_tokens + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                  + (getattr(usage, "cache_creation_input_tokens", 0) or 0))
        _, p_in, p_out = next(t for t in HAIKU_PRICES if prompt <= t[0])
        return (prompt * p_in + usage.output_tokens * p_out) / 1_000_000
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        usage.input_tokens * PRICE_IN
        + usage.output_tokens * PRICE_OUT
        + cache_read * PRICE_CACHE_READ
        + cache_write * PRICE_CACHE_WRITE
    ) / 1_000_000


def call_json(client: anthropic.Anthropic, content, schema: dict, effort: str, system: str, max_tokens: int = 64000,
              model: str = MODEL):
    """One streamed Claude call constrained to a JSON schema. Returns (parsed_json, cost_usd)."""
    import json

    extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model == MODEL else {}
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
        **extra,
    ) as stream:
        msg = stream.get_final_message()

    cost = usage_cost(msg.usage, model)
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request ({getattr(msg.stop_details, 'category', None)}).")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude's response hit the output limit before finishing.")
    text = next(b.text for b in msg.content if b.type == "text")
    return json.loads(text), cost


def site_slug(url: str) -> str:
    p = urlparse(url)
    return re.sub(r"[^a-zA-Z0-9]+", "_", (p.netloc + p.path)).strip("_")[:80]


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name).strip()
    return name[:150] or "document"
