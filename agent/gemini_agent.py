"""
Alternative tool-using agent loop using Google Gemini (free tier via Google
AI Studio) instead of the Anthropic API -- for when you'd rather not add
paid credit to run this assessment. The assessment brief explicitly allows
choosing your own model provider.

Uses google-generativeai's automatic function calling for each individual
call: we hand it plain Python functions (wrapping the same
JiraToolBackend/MockToolBackend used by the Anthropic path) and the SDK
executes the tool-call round-trips for that one call internally. That
internal loop has its own (undocumented, not independently configurable)
iteration cap, which is too low to reliably let the model follow a
multi-hop link graph to exhaustion in a single call. So we drive it in an
OUTER loop ourselves: after each call, we check whether the model actually
produced the final JSON report; if not, we send an explicit instruction to
keep investigating (covering any linked records it hasn't opened yet)
before concluding, and only stop once we get a parseable final report or
hit our own outer-round budget. Every underlying tool call -- across every
outer round -- is still logged to the same outputs/investigation_trace.jsonl
format, since the wrapped functions append to a shared trace list as a
side effect regardless of which round they ran in.
"""
from __future__ import annotations

import json
import os
import re
import time

from agent.system_prompt import SYSTEM_PROMPT

MAX_RETRIES = 10
MAX_OUTER_ROUNDS = 8

_RETRY_DELAY_PATTERN = re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.IGNORECASE)
_RETRY_IN_PATTERN = re.compile(r"retry in\s*([\d.]+)\s*s", re.IGNORECASE)

CONTINUE_NUDGE = (
    "Before finalizing, double check: have you called get_issue on every record "
    "reachable (directly or through further links) from every one of the 12 "
    "requirements? If not, continue investigating now -- call get_issue on any "
    "linked records you haven't opened yet, including links found via other "
    "evidence records, not just the ones linked straight from the requirement. "
    "Only once you are certain you've exhausted the reachable records for every "
    "requirement, respond with ONLY the final JSON report as specified in your "
    "instructions -- no prose, no markdown fences, nothing else."
)


def _suggested_retry_seconds(error_text: str) -> float:
    """Google's 429 errors tell you exactly how long to wait, either as a
    structured `retry_delay { seconds: N }` field or a "Please retry in
    N.NNs" sentence. Honour that instead of guessing with our own backoff
    schedule -- a requests-PER-MINUTE limit (as opposed to per-day) is
    usually only ~30-60s away from clearing, and a fixed short backoff
    (e.g. 5s, 10s, 20s...) can burn through all our retries while still
    inside the same one-minute window, failing the whole run over a limit
    that was about to clear anyway."""
    match = _RETRY_DELAY_PATTERN.search(error_text) or _RETRY_IN_PATTERN.search(error_text)
    if match:
        return float(match.group(1)) + 2  # small buffer past what Google suggested
    return 15.0  # reasonable default if we can't parse a suggestion out of the error


def _with_retry(fn, *args, **kwargs):
    """Free-tier Gemini keys have low requests-per-minute/day limits; back
    off and retry on 429s instead of failing the whole run."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            error_text = str(e)
            is_rate_limit = (
                "429" in error_text or "quota" in error_text.lower() or "RESOURCE_EXHAUSTED" in error_text
            )
            if is_rate_limit and attempt < MAX_RETRIES - 1:
                wait = _suggested_retry_seconds(error_text)
                print(f"    (rate limited, waiting {wait:.0f}s before retry {attempt + 1}/{MAX_RETRIES - 1})")
                time.sleep(wait)
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
        """List the requirement records (source_id, key, summary, status) for
        this import. Use each result's source_id (e.g. "REQ-05"), not its Jira
        key, as the requirement's identity in your reasoning and final output."""
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
        """Search issues within this import's scope using a JQL fragment built
        ONLY from `labels = "value"` clauses ANDed together, e.g.
        'labels = "kind-work" AND labels = "release-R2.4"'. Always scoped
        automatically to this dataset's project/label; do not add your own
        project/dataset-label clause, and do not use any other JQL operator --
        anything outside this shape is rejected."""
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
        """Fetch one issue's full detail: source_id, summary, description, status,
        comments, and its linked issues (key + source_id + summary + link
        direction/type). Use this to read evidence AND to discover further
        records to follow via `links` -- follow every link you haven't opened
        yet, including links reached through other evidence, not just the ones
        linked straight from the requirement."""
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


def _try_parse_final_json(text):
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    if not cleaned.startswith("{"):
        return None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if "overall_decision" in parsed and "requirements" in parsed:
        return parsed
    return None


def run_agent_gemini(backend, model_name: str, max_outer_rounds: int = MAX_OUTER_ROUNDS):
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
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    final_report = None
    final_text = None

    message = (
        "Begin the investigation now and produce the final release-readiness "
        "report as specified in your instructions."
    )

    for _round in range(max_outer_rounds):
        response = _with_retry(chat.send_message, message)

        try:
            usage_totals["input_tokens"] += response.usage_metadata.prompt_token_count
            usage_totals["output_tokens"] += response.usage_metadata.candidates_token_count
        except Exception:  # noqa: BLE001 - usage metadata is best-effort
            pass

        final_text = response.text if hasattr(response, "text") else None
        final_report = _try_parse_final_json(final_text)
        if final_report is not None:
            break

        message = CONTINUE_NUDGE

    elapsed = time.time() - start
    # Return the raw text either way; agent.py's parse_final_json does its
    # own parsing/validation pass regardless.
    return final_text, trace, usage_totals, elapsed
