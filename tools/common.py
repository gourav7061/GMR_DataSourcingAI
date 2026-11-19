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
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# USD per million tokens for claude-opus-5-5
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 4.00, 20.00, 0.20, 5.00


def get_client() -> anthropic.Anthropic:
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add a line ANTHROPIC_API_KEY=sk-ant-... to the .env file."
        )
    return anthropic.Anthropic(max_retries=4)


def usage_cost(usage) -> float:
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        usage.input_tokens * PRICE_IN
        + usage.output_tokens * PRICE_OUT
        + cache_read * PRICE_CACHE_READ
        + cache_write * PRICE_CACHE_WRITE
    ) / 1_000_000


def call_json(client: anthropic.Anthropic, content, schema: dict, effort: str, system: str, max_tokens: int = 64000):
    """One streamed Claude call constrained to a JSON schema. Returns (parsed_json, cost_usd)."""
    import json

    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()

    cost = usage_cost(msg.usage)
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
