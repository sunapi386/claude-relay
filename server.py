#!/usr/bin/env python3
"""OpenAI-compatible API server that proxies requests through the Claude CLI.

Uses your authenticated Claude Code CLI (Pro Max subscription) as the backend,
exposed as an OpenAI-compatible /v1/chat/completions endpoint so tools like
OpenClaw can use it.

Prompt is passed via stdin to avoid shell injection and argument length limits.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
import uuid

from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", shutil.which("claude") or "claude")
DEFAULT_TIMEOUT = 300


def _run_claude(prompt, model=None, system_prompt=None):
    """Run claude CLI and return the parsed JSON output."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT,
    )

    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr.strip()}")

    return json.loads(result.stdout)


def _run_claude_stream(prompt, model=None, system_prompt=None):
    """Run claude CLI in streaming mode, yielding text chunks."""
    cmd = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdin.write(prompt)
    proc.stdin.close()

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = event.get("type", "")

            if msg_type == "assistant" and "content" in event:
                for block in event.get("content", []):
                    if block.get("type") == "text":
                        yield block["text"]

            elif msg_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta["text"]

            elif msg_type == "result":
                yield None  # signal done
    finally:
        proc.stdout.close()
        proc.stderr.close()
        proc.wait()


def _messages_to_prompt(messages):
    """Convert OpenAI messages array to a single prompt string."""
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Handle content blocks (text only)
            content = " ".join(
                block.get("text", "")
                for block in content
                if block.get("type") == "text"
            )
        if role == "system":
            parts.append(f"[System] {content}")
        elif role == "assistant":
            parts.append(f"[Assistant] {content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


def _extract_system_prompt(messages):
    """Extract system message from messages array."""
    system_parts = []
    other_messages = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                )
            system_parts.append(content)
        else:
            other_messages.append(msg)
    system_prompt = "\n".join(system_parts) if system_parts else None
    return system_prompt, other_messages


@app.route("/v1/models", methods=["GET"])
def list_models():
    """List available models."""
    return jsonify({
        "object": "list",
        "data": [
            {
                "id": "claude-opus-4-20250918",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "anthropic",
            },
            {
                "id": "claude-sonnet-4-20250514",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "anthropic",
            },
        ],
    })


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """OpenAI-compatible chat completions endpoint."""
    data = request.get_json(force=True)
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": {"message": "messages is required"}}), 400

    model = data.get("model", "claude-opus-4-20250918")
    stream = data.get("stream", False)

    system_prompt, user_messages = _extract_system_prompt(messages)
    prompt = _messages_to_prompt(user_messages)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if stream:
        def generate():
            # SSE: send role chunk
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk)}\n\n"

            try:
                for text in _run_claude_stream(prompt, model=model, system_prompt=system_prompt):
                    if text is None:
                        break
                    chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as e:
                error_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": f"\n[Error: {e}]"},
                        "finish_reason": "stop",
                    }],
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"

            # Final chunk
            done_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
            }
            yield f"data: {json.dumps(done_chunk)}\n\n"
            yield "data: [DONE]\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            parsed = _run_claude(prompt, model=model, system_prompt=system_prompt)
        except Exception as e:
            return jsonify({"error": {"message": str(e)}}), 502

        result_text = parsed.get("result", "")
        return jsonify({
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result_text,
                },
                "finish_reason": "stop",
            }],
            "usage": parsed.get("usage", {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }),
        })


@app.route("/v1/health", methods=["GET"])
def health():
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({"status": "ok", "claude_version": result.stdout.strip()})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 503


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Claude CLI → OpenAI-compatible API relay")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    print(f"Claude relay server on {args.host}:{args.port}")
    print(f"Claude CLI: {CLAUDE_BIN}")
    print(f"Endpoints: /v1/chat/completions, /v1/models, /v1/health")
    app.run(host=args.host, port=args.port, debug=False)
