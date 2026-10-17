"""
tracing.py — W3C trace-context propagation from Engram runs into Claude Code.

One trace per pipeline run, one span per step:

    invoke_workflow engram-tier-<n>        run JSON: trace_id, span_id
      └─ invoke_agent <role>               run JSON: pipeline[i].span_id
           └─ claude_code.interaction …    (Claude Code, parented via TRACEPARENT)

Each step's Claude Code process gets

    TRACEPARENT=00-<run trace_id>-<step span_id>-01

`claude -p` reads an inbound TRACEPARENT and parents its own spans under it
(Claude Code monitoring docs; traces need CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1).
Bash subprocesses inherit it too.

Two layers, both optional:

1. **Propagation + Claude Code telemetry env** (stdlib only). Always computes
   the ids and TRACEPARENT. When `telemetry.claude_code.enabled` is set, it also
   adds CLAUDE_CODE_ENABLE_TELEMETRY=1, the OTEL_*_EXPORTER vars and the OTLP
   endpoint/protocol, and, with `enhanced_traces_beta`,
   CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 + OTEL_TRACES_EXPORTER.
2. **Engram's own spans** (`telemetry.engram_spans: true`, soft dependency on
   opentelemetry-sdk + opentelemetry-exporter-otlp). Emits real
   `invoke_workflow` / `invoke_agent` spans (OTel GenAI agent-span conventions,
   Development status) so the Claude Code spans hang off a real parent. If the
   SDK isn't installed this layer is a silent no-op and ids come from `secrets`.

How the env reaches Claude Code: agent_runner passes it to the step's tmux
session with `tmux new-session -e KEY=VAL` (tmux >= 3.0) AND writes it to the
task JSON as `"env": {...}`. ~/raphael/agents/run-agent.sh (Mac Mini, not in
this repo) must not scrub the environment before `claude -p`; to be robust to
a long-lived tmux server it should also export the task JSON's env:

    eval "$(jq -r '.env // {} | to_entries[] | "export \\(.key)=\\(.value|@sh)"' "$TASK_FILE")"

Secrets (OTLP auth headers) never go in config.yaml or the task JSON: list
the variable names under `telemetry.passthrough_env` and they're copied from
the daemon's own environment into the tmux session only.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from typing import Mapping, Optional

log = logging.getLogger("engram")

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

# ── Optional OpenTelemetry SDK ────────────────────────────────────────────────
try:  # pragma: no cover - depends on the host having the SDK
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, SpanKind, TraceFlags

    _OTEL_AVAILABLE = True
except Exception:  # ImportError, or a broken install
    _OTEL_AVAILABLE = False

_tracer = None  # set by init() when engram_spans is on and the SDK is importable
_spans: dict = {}  # (run_id, step_idx | None) -> live SDK span
_spans_lock = threading.Lock()


# ── Pure helpers ──────────────────────────────────────────────────────────────


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(value: str) -> Optional[tuple]:
    m = _TRACEPARENT_RE.match((value or "").strip())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None
    return m.group(1), m.group(2), m.group(3)


def _section(config: Optional[dict]) -> dict:
    return dict((config or {}).get("telemetry") or {})


# ── Engram spans (soft dependency) ────────────────────────────────────────────


def init(config: Optional[dict]) -> bool:
    """Set up an OTLP TracerProvider for Engram's own spans if configured and
    the SDK is installed. Safe to call more than once. Returns True if active."""
    global _tracer
    if not _section(config).get("engram_spans") or not _OTEL_AVAILABLE:
        _tracer = None
        return False
    try:  # pragma: no cover - needs opentelemetry-sdk
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        cc = dict(_section(config).get("claude_code") or {})
        protocol = cc.get("otlp_protocol") or "http/protobuf"
        if protocol == "grpc":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[no-redef]
                OTLPSpanExporter,
            )
        endpoint = cc.get("otlp_endpoint") or None
        if endpoint and protocol != "grpc":
            endpoint = endpoint.rstrip("/") + "/v1/traces"
        provider = TracerProvider(resource=Resource.create({"service.name": "engram"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        _tracer = provider.get_tracer("engram")
        log.info("[tracing] Engram OTel spans enabled (%s)", protocol)
        return True
    except Exception as exc:
        log.warning(f"[tracing] OTel SDK present but exporter setup failed, spans off: {exc}")
        _tracer = None
        return False


def _sdk_ids(span) -> Optional[tuple]:  # pragma: no cover - needs SDK
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def start_workflow(run_id: str, name: str, attributes: Optional[dict] = None) -> tuple:
    """Start the run's root span. Returns (trace_id, span_id) as hex strings."""
    if _tracer is not None:  # pragma: no cover - needs SDK
        try:
            span = _tracer.start_span(
                f"invoke_workflow {name}",
                kind=SpanKind.INTERNAL,
                attributes={
                    "gen_ai.operation.name": "invoke_workflow",
                    "gen_ai.workflow.name": name,
                    "gen_ai.conversation.id": run_id,
                    **(attributes or {}),
                },
            )
            ids = _sdk_ids(span)
            if ids:
                with _spans_lock:
                    _spans[(run_id, None)] = span
                return ids
        except Exception as exc:
            log.warning(f"[tracing] start_workflow failed, falling back to bare ids: {exc}")
    return new_trace_id(), new_span_id()


def start_step(
    run_id: str,
    idx: int,
    role: str,
    trace_id: str,
    parent_span_id: Optional[str],
    attributes: Optional[dict] = None,
) -> str:
    """Start a step's invoke_agent span under the run's span. Returns span_id.
    A retry of the same step ends the previous attempt's span first."""
    end_step(run_id, idx, "retried")
    if _tracer is not None:  # pragma: no cover - needs SDK
        try:
            with _spans_lock:
                parent = _spans.get((run_id, None))
            if parent is not None:
                ctx = _otel_trace.set_span_in_context(parent)
            elif parent_span_id:
                ctx = _otel_trace.set_span_in_context(
                    NonRecordingSpan(
                        SpanContext(
                            int(trace_id, 16),
                            int(parent_span_id, 16),
                            is_remote=True,
                            trace_flags=TraceFlags(TraceFlags.SAMPLED),
                        )
                    )
                )
            else:
                ctx = None
            span = _tracer.start_span(
                f"invoke_agent {role}",
                context=ctx,
                kind=SpanKind.CLIENT,  # the agent runs out-of-process (claude -p)
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.agent.name": role,
                    "gen_ai.provider.name": "anthropic",
                    "gen_ai.conversation.id": run_id,
                    **(attributes or {}),
                },
            )
            ids = _sdk_ids(span)
            if ids and ids[0] == trace_id:
                with _spans_lock:
                    _spans[(run_id, idx)] = span
                return ids[1]
            span.end()
        except Exception as exc:
            log.warning(f"[tracing] start_step failed, falling back to a bare span id: {exc}")
    return new_span_id()


def _end(key: tuple, status: str, attributes: Optional[dict] = None) -> None:
    with _spans_lock:
        span = _spans.pop(key, None)
    if span is None:
        return
    try:  # pragma: no cover - needs SDK
        from opentelemetry.trace import Status, StatusCode

        span.set_attribute("engram.status", status)
        for k, v in (attributes or {}).items():
            if v is not None:
                span.set_attribute(k, v)
        if status in ("failed", "timeout", "rate-limited"):
            span.set_status(Status(StatusCode.ERROR, status))
            span.set_attribute("error.type", status)
        span.end()
    except Exception as exc:
        log.debug(f"[tracing] ending span {key} failed: {exc}")


def end_step(run_id: str, idx: int, status: str, attributes: Optional[dict] = None) -> None:
    _end((run_id, idx), status, attributes)


def end_workflow(run_id: str, status: str, attributes: Optional[dict] = None) -> None:
    """End any step spans still open for the run, then the run span."""
    with _spans_lock:
        step_keys = [k for k in _spans if k[0] == run_id and k[1] is not None]
    for k in step_keys:
        _end(k, status)
    _end((run_id, None), status, attributes)


# ── Env for the Claude Code process ───────────────────────────────────────────


def step_env(
    config: Optional[dict],
    trace_id: Optional[str],
    span_id: Optional[str],
    run_id: str = "",
    role: str = "",
    environ: Optional[dict] = None,
) -> tuple:
    """Build the env for one step's `claude -p`.

    Returns (public_env, secret_env): public_env is safe to persist in the task
    JSON; secret_env (telemetry.passthrough_env values copied from the daemon's
    own environment) only goes to the tmux session.
    """
    sec = _section(config)
    src: Mapping[str, str] = os.environ if environ is None else environ
    env: dict = {}

    if trace_id and span_id and sec.get("propagate_traceparent", True):
        env["TRACEPARENT"] = format_traceparent(trace_id, span_id)

    cc = dict(sec.get("claude_code") or {})
    if cc.get("enabled"):
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        env["OTEL_METRICS_EXPORTER"] = str(cc.get("metrics_exporter") or "otlp")
        env["OTEL_LOGS_EXPORTER"] = str(cc.get("logs_exporter") or "otlp")
        if cc.get("enhanced_traces_beta"):
            env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
            env["OTEL_TRACES_EXPORTER"] = str(cc.get("traces_exporter") or "otlp")
        if cc.get("otlp_endpoint"):
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = str(cc["otlp_endpoint"])
        if cc.get("otlp_protocol"):
            env["OTEL_EXPORTER_OTLP_PROTOCOL"] = str(cc["otlp_protocol"])
        attrs = [f"engram.run_id={run_id}"] if run_id else []
        if role:
            attrs.append(f"engram.role={role}")
        if cc.get("resource_attributes"):
            attrs.append(str(cc["resource_attributes"]))
        if attrs:
            env["OTEL_RESOURCE_ATTRIBUTES"] = ",".join(attrs)
        for k, v in dict(cc.get("extra_env") or {}).items():
            env[str(k)] = str(v)

    secret_env = {name: src[name] for name in (sec.get("passthrough_env") or []) if src.get(name)}
    return env, secret_env
