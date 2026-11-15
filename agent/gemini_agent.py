"""
Alternative tool-using agent loop using Google Gemini (free tier via Google
AI Studio) instead of the Anthropic API -- for when you'd rather not add
paid credit to run this assessment. The assessment brief explicitly allows
choosing your own model provider.

Uses google-generativeai's automatic function calling: we hand it plain
Python functions (wrapping the same JiraToolBackend/MockToolBackend used by
the Anthropic path) and the SDK executes the whole tool-use loop for us in
one send_message() call. We still log every underlying call to the same
outputs/investigation_trace.jsonl format by having each wrapped function
append to a shared trace list as a side effect.
"""
from __future__ import annotations

import json
import os
import time

from agent.system_prompt import SYSTEM_PROMPT

MAX_RETRIES = 5


def _with_retry(fn, *args, **kwargs):
    """Free-tier Gemini keys have low requests-per-minute limits; back off
    and retry on 429s instead of failing the whole run."""
    delay = 5
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            if "429" in str(e) or "quota" in str(e).lower():
                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(delay)
                delay *= 2
            else:
                raise


def build_tool_functions(backend, trace: list):
    """Wrap backend methods as plain functions with docstrings, which
    google-generativeai introspects to build the tool schema automatically."""

    def get_manifest() -> dict:
        """Fetch the release manifest record for this import: the target build
        (release, commit, prompt version, environment, service) and the recorded
        assessment rules. Call this first."""
        t0 = time.time()
        result = backend.get_manifest()
        trace.append(
            {
                "tool": "get_manifest",
                "input": {},
                "result": result,
                "is_error": isinstance(result, dict) and "error" in result,
                "elapsed_seconds": round(time.time() - t0, 3),
            }
        )
        return result

    def get_requirements() -> list:
        """List the requirement records (key + summary + status) for this import."""
        t0 = time.time()
        result = backend.get_requirements()
        trace.append(
            {
                "tool": "get_requirements",
                "input": {},
                "result": result,
                "is_error": False,
                "elapsed_seconds": round(time.time() - t0, 3),
            }
        )
        return result

    def search_issues(jql: str, max_results: int = 50) -> list:
        """Search issues within this import's scope using a JQL fragment, e.g.
        'labels = "kind-work" AND labels = "release-R2.4"'. Always scoped
        automatically to this dataset's project/label; do not add your own
        project/dataset-label clause."""
        t0 = time.time()
        result = backend.search_issues(jql, max_results)
        trace.append(
            {
                "tool": "search_issues",
                "input": {"jql": jql, "max_results": max_results},
                "result": result,
                "is_error": False,
                "elapsed_seconds": round(time.time() - t0, 3),
            }
        )
        return result

    def get_issue(key: str) -> dict:
        """Fetch one issue's full detail: summary, description, status, comments,
        and its linked issues (key + summary + link direction/type). Use this to
        read evidence and to discover further records to follow via `links`."""
        t0 = time.time()
        result = backend.get_issue(key)
        trace.append(
            {
                "tool": "get_issue",
                "input": {"key": key},
                "result": result,
                "is_error": isinstance(result, dict) and "error" in result,
                "elapsed_seconds": round(time.time() - t0, 3),
            }
        )
        return result

    return [get_manifest, get_requirements, search_issues, get_issue]


def run_agent_gemini(backend, model_name: str):
    import google.generativeai as genai

    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])

    trace: list = []
    tool_fns = build_tool_functions(backend, trace)

    model = genai.GenerativeModel(
        model_name,
        system_instruction=SYSTEM_PROMPT,
        tools=tool_fns,
    )
    chat = model.start_chat(enable_automatic_function_calling=True)

    start = time.time()
    response = _with_retry(
        chat.send_message,
        "Begin the investigation now and produce the final release-readiness "
        "report as specified in your instructions.",
    )
    elapsed = time.time() - start

    final_text = response.text if hasattr(response, "text") else None

    usage = {"input_tokens": 0, "output_tokens": 0}
    try:
        usage = {
            "input_tokens": response.usage_metadata.prompt_token_count,
            "output_tokens": response.usage_metadata.candidates_token_count,
        }
    except Exception:  # noqa: BLE001 - usage metadata is best-effort
        pass

    return final_text, trace, usage, elapsed
