"""Thin wrapper for schema-constrained JSON calls to Claude."""
from __future__ import annotations

import json
import sys

DEFAULT_MODEL = "claude-opus-5-5"
# Bulk reading (one call per transcript) and web discovery: faster and cheaper.
FAST_MODEL = "claude-sonnet-5-5"
# Server-side refusal fallback: if the model's safety classifier declines,
# the API retries on Anthropic's recommended fallback model in the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def make_client():
    import anthropic

    return anthropic.Anthropic()


def call_json(
    client,
    *,
    model: str,
    system: str,
    user: str,
    schema: dict,
    effort: str = "medium",
    max_tokens: int = 32000,
) -> dict | None:
    """Return the parsed JSON object, or None if the request was refused."""
    with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        betas=[FALLBACK_BETA],
        extra_body={"fallbacks": "default"},
    ) as stream:
        msg = stream.get_final_message()

    if msg.stop_reason == "refusal":
        details = getattr(msg, "stop_details", None)
        print(f"  request declined ({getattr(details, 'category', None)}); skipping", file=sys.stderr)
        return None
    if msg.stop_reason == "max_tokens":
        raise LLMError("response hit max_tokens before the JSON was complete")

    # If a fallback ran mid-stream, the declined model's partial text precedes
    # a `fallback` block; only the text after the last one is the answer.
    text_parts: list[str] = []
    for block in msg.content:
        if block.type == "fallback":
            text_parts = []
        elif block.type == "text":
            text_parts.append(block.text)
    text = "".join(text_parts).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"model returned invalid JSON: {e}") from e
