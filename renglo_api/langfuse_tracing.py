"""
Langfuse trace enrichment for the chat/agent entry point.

The drop-in ``langfuse.openai`` wrapper captures every OpenAI call, but without an
enclosing trace each completion is a flat, ungrouped observation. This module provides
a single context manager that wraps a chat request so all completions in one turn roll
up under one named trace carrying ``session_id`` / ``user_id`` / ``tags`` / ``metadata``
— which is what makes traces filterable for dataset curation, LLM-as-a-judge, and
session replay.

Fully no-op (and never raises) when Langfuse is not configured. Observability must
never break a chat request.
"""

import os
from contextlib import contextmanager

from flask import g, has_request_context


def _tracing_enabled() -> bool:
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    )


@contextmanager
def trace_chat_request(payload: dict):
    """Wrap an agent dispatch so nested OpenAI generations attach to one enriched trace.

    Yields the active Langfuse span (or ``None`` when tracing is disabled). Identifiers
    are read from the flat chat payload (same keys agent_core / agent_react consume).
    """
    if not _tracing_enabled() or not isinstance(payload, dict):
        yield None
        return

    try:
        from langfuse import get_client, propagate_attributes

        lf = get_client()
        thread = payload.get("thread") or payload.get("entity_id") or ""
        channel = "whatsapp" if str(thread).startswith("wa:") else "web"
        core = payload.get("core") or "triage"

        # v4 coerces metadata values to strings; drop None so we don't store "None".
        metadata = {
            k: v
            for k, v in {
                "portfolio": payload.get("portfolio"),
                "org": payload.get("org"),
                "entity_type": payload.get("entity_type"),
                "entity_id": payload.get("entity_id"),
                "connection_id": payload.get("connectionId"),
            }.items()
            if v is not None
        }

        trace_name = f"chat.{core}"
        if has_request_context():
            g.langfuse_dirty = True
        with lf.start_as_current_observation(as_type="span", name=trace_name) as span:
            span.update(input=payload.get("data"))
            with propagate_attributes(
                user_id=str(payload.get("public_user") or ""),
                session_id=str(thread),
                tags=[os.environ.get("SYS_ENV", "dev"), channel, f"core:{core}"],
                metadata=metadata,
                trace_name=trace_name,
            ):
                yield span
    except Exception:
        # Never let observability break a chat request.
        yield None
