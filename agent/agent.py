"""
The tool-using release-readiness agent.

Usage:
    python -m agent.agent --backend jira
    python -m agent.agent --backend mock --dataset inputs/dataset.json   # dev-only dry run

Writes:
    outputs/investigation_trace.jsonl   every tool call + result, in order, with timings
    outputs/release_report.json         the model's final structured decision
    outputs/release_report.md           the same, rendered as a readable report
    outputs/run_stats.json              wall-clock time + token usage
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time

from dotenv import load_dotenv

from agent.system_prompt import SYSTEM_PROMPT

load_dotenv()

TOOLS = [
    {
        "name": "get_manifest",
        "description": (
            "Fetch the release manifest record for this import: the target build "
            "(release, commit, prompt version, environment, service) and the recorded "
            "assessment rules. Call this first."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_requirements",
        "description": (
            "List the requirement records (source_id, key, summary, status) for this "
            "import. Use each result's source_id (e.g. \"REQ-05\"), not its Jira key, as "
            "the requirement's identity in your reasoning and in your final output."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_issues",
        "description": (
            "Search issues within this import's scope using a JQL fragment built ONLY "
            'from `labels = "value"` clauses ANDed together, e.g. '
            '\'labels = "kind-work" AND labels = "release-R2.4"\'. '
            "Always scoped automatically to this dataset's project/label; do not add "
            "your own project/dataset-label clause, and do not use any other JQL "
            "operator -- anything outside this shape is rejected."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string", "description": "A JQL fragment, or empty string to list everything in scope."},
                "max_results": {"type": "integer", "description": "Default 50."},
            },
            "required": ["jql"],
        },
    },
    {
        "name": "get_issue",
        "description": (
            "Fetch one issue's full detail: source_id, summary, description, status, "
            "comments, and its linked issues (key + source_id + summary + link "
            "direction/type). Use this to read evidence AND to discover further records "
            "to follow via `links` -- follow every link you haven't opened yet, "
            "including links reached through other evidence, not just the ones linked "
            "straight from the requirement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
]


def make_backend(args):
    if args.backend == "jira":
        from agent.jira_tools import JiraToolBackend

        return JiraToolBackend.from_env()
    else:
        from agent.mock_tools import MockToolBackend

        return MockToolBackend.from_dataset(args.dataset)


def execute_tool(backend, name: str, tool_input: dict):
    if name == "get_manifest":
        return backend.get_manifest()
    if name == "get_requirements":
        return backend.get_requirements()
    if name == "search_issues":
        return backend.search_issues(tool_input.get("jql", ""), tool_input.get("max_results", 50))
    if name == "get_issue":
        return backend.get_issue(tool_input["key"])
    return {"error": f"Unknown tool {name}"}


def run_agent(backend, model: str, max_iterations: int = 40):
    import anthropic

    client = anthropic.Anthropic()

    messages = [
        {
            "role": "user",
            "content": (
                "Begin the investigation now and produce the final release-readiness "
                "report as specified in your instructions."
            ),
        }
    ]

    trace = []
    usage_totals = {"input_tokens": 0, "output_tokens": 0}
    start = time.time()

    final_text = None
    for iteration in range(max_iterations):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=TOOLS,
        )
        usage_totals["input_tokens"] += response.usage.input_tokens
        usage_totals["output_tokens"] += response.usage.output_tokens

        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason != "tool_use" or not tool_calls:
            text_blocks = [b.text for b in response.content if b.type == "text"]
            final_text = "\n".join(text_blocks).strip()
            break

        tool_results = []
        for call in tool_calls:
            call_start = time.time()
            try:
                result = execute_tool(backend, call.name, call.input)
                is_error = isinstance(result, dict) and "error" in result and len(result) == 1
            except Exception as e:  # noqa: BLE001 - we want to report any failure to the model and the trace
                result = {"error": f"Tool execution failed: {e}"}
                is_error = True
            elapsed = time.time() - call_start

            trace.append(
                {
                    "iteration": iteration,
                    "tool": call.name,
                    "input": call.input,
                    "result": result,
                    "is_error": is_error,
                    "elapsed_seconds": round(elapsed, 3),
                }
            )

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": json.dumps(result, ensure_ascii=False),
                    "is_error": is_error,
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = None  # hit max_iterations without the model stopping

    elapsed_total = time.time() - start
    return final_text, trace, usage_totals, elapsed_total


def parse_final_json(text: str | None) -> dict:
    if not text:
        return {"error": "Agent did not produce a final answer within the iteration budget."}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {"error": f"Could not parse model output as JSON: {e}", "raw_text": text}


_SCOPE_VALUE_PATTERNS = {
    "release": re.compile(r"\brelease\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    "commit": re.compile(r"\bcommit\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    "service": re.compile(r"\bservice\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    "environment": re.compile(r"\benvironment\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    "prompt": re.compile(r"\bprompt(?:\s+(?:bundle|version))?\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
}


def _qa_comments(issue: dict) -> list[str]:
    return [c for c in issue.get("comments", []) if isinstance(c, str) and re.search(r"\|\s*role\s+QA\b", c, re.IGNORECASE)]


def _qa_result_groups(issue: dict) -> list[tuple[str, str]]:
    comments = _qa_comments(issue)
    if not comments:
        return [("no QA-authored result", "")]
    groups = {}
    for comment in comments:
        match = re.search(r"\b(?:EVAL|RB|TEST|RUN|REG|PARSE)-\d+\b", comment, re.IGNORECASE)
        group_id = match.group(0).upper() if match else "unkeyed"
        groups.setdefault(group_id, []).append(comment)
    return [(group_id, "\n".join(group_comments)) for group_id, group_comments in groups.items()]


def _scope_mismatches_text(text: str, target: dict, require_prompt: bool) -> list[str]:
    if not text:
        return ["no QA-authored result"]
    fields = ["release", "commit", "service", "environment"] + (["prompt"] if require_prompt else [])
    mismatches = []
    for field in fields:
        expected = str(target.get(field, "")).strip().lower()
        values = [v.rstrip(".,;:)]}").strip().lower() for v in _SCOPE_VALUE_PATTERNS[field].findall(text)]
        if not expected:
            mismatches.append(f"target {field} is missing")
        elif not values:
            mismatches.append(f"{field} is not stated")
        elif any(v != expected for v in values):
            mismatches.append(f"{field}={', '.join(dict.fromkeys(values))} (target {target[field]})")
    return mismatches


def _issue_cache_from_trace(trace: list) -> dict:
    cache = {}
    for entry in trace:
        if entry.get("tool") != "get_issue":
            continue
        result = entry.get("result")
        if isinstance(result, dict) and not result.get("error"):
            if result.get("key"):
                cache[result["key"]] = result
            if result.get("source_id"):
                cache[result["source_id"]] = result
    return cache


def _fetch_issue_for_guard(key: str, backend, trace: list, cache: dict, notes: list):
    if key in cache:
        return cache[key]
    started = time.time()
    try:
        result = backend.get_issue(key)
        is_error = isinstance(result, dict) and bool(result.get("error"))
    except Exception as exc:  # noqa: BLE001 - keep the audit failure visible in the report
        result = {"error": f"Post-run evidence audit could not fetch {key}: {exc}"}
        is_error = True
    trace.append({"tool": "get_issue", "input": {"key": key}, "result": result,
                  "is_error": is_error, "elapsed_seconds": round(time.time() - started, 3),
                  "validator_fetch": True})
    if not is_error:
        if result.get("key"):
            cache[result["key"]] = result
        if result.get("source_id"):
            cache[result["source_id"]] = result
    else:
        notes.append(f"Evidence guard could not inspect linked issue {key}")
    return result


def _direct_evidence_for_requirement(source_id: str, known_requirements: list, backend, trace: list, cache: dict, notes: list) -> list[dict]:
    requirement = next((r for r in known_requirements if r.get("source_id") == source_id), None)
    if not requirement or not requirement.get("key"):
        return []
    issue = _fetch_issue_for_guard(requirement["key"], backend, trace, cache, notes)
    if not isinstance(issue, dict) or issue.get("error"):
        return []
    evidence = {}
    for link in issue.get("links", []):
        key = link.get("key")
        if not key:
            continue
        linked = _fetch_issue_for_guard(key, backend, trace, cache, notes)
        if not isinstance(linked, dict) or linked.get("error") or "kind-evidence" not in (linked.get("labels") or []):
            continue
        if linked.get("source_id"):
            evidence[linked["source_id"]] = linked
    return list(evidence.values())


def _evidence_ref(issue: dict, note: str) -> dict | None:
    source_id = issue.get("source_id")
    return {"key": source_id, "note": note} if source_id else None


def _apply_evidence_consistency_guards(report: dict, backend, known_requirements: list, trace: list, notes: list) -> None:
    """Conservatively prevent PASS when linked QA scope or outcomes contradict it."""
    target = report.get("target", {})
    cache = _issue_cache_from_trace(trace)
    for result in report.get("requirements", []):
        if result.get("decision") != "PASS":
            continue
        source_id = result.get("requirement_key")
        requirement = next((r for r in known_requirements if r.get("source_id") == source_id), None)
        summary = str((requirement or {}).get("summary") or result.get("requirement_summary") or "").lower()
        prompt_quality = "prompt" in summary and "quality" in summary
        rollback = "rollback" in summary and "rehearsal" in summary
        if not (prompt_quality or rollback):
            continue

        evidence = _direct_evidence_for_requirement(source_id, known_requirements, backend, trace, cache, notes)
        if prompt_quality:
            checked = []
            for issue in evidence:
                for group_id, text in _qa_result_groups(issue):
                    checked.append((issue, group_id, text, _scope_mismatches_text(text, target, require_prompt=True)))
            if any(not mismatches for _, _, _, mismatches in checked):
                continue
            refs_by_source, details = {}, []
            for issue, group_id, _, mismatches in checked:
                source = issue.get("source_id", "unknown evidence")
                detail = "; ".join(mismatches) or "scope could not be verified"
                details.append(f"{source} ({group_id}): {detail}")
                refs_by_source[source] = _evidence_ref(issue, f"{group_id} not applicable: {detail}")
            result["decision"] = "INSUFFICIENT_EVIDENCE"
            result["explanation"] = (
                "No linked QA evaluation establishes the required prompt-quality result on the full target. "
                + ("; ".join(details) if details else "No directly linked QA evidence was available.")
                + ". The manifest requires the release, commit, service, production environment, and deployed prompt to match."
            )
            result["evidence_refs"] = [ref for ref in refs_by_source.values() if ref]
            notes.append(f"{source_id}: evidence guard changed PASS to INSUFFICIENT_EVIDENCE because no linked QA evaluation matched the full prompt target")
            continue

        scoped, qa_text, refs = [], [], []
        for issue in evidence:
            comments = _qa_comments(issue)
            qa_text.extend(comments)
            for group_id, text in _qa_result_groups(issue):
                mismatches = _scope_mismatches_text(text, target, require_prompt=False)
                if mismatches:
                    refs.append(_evidence_ref(issue, f"{group_id} not applicable to target configuration: " + "; ".join(mismatches)))
                    continue
                no_loss_removed = re.sub(r"\bno\s+(?:data|session|records?)\s+loss\b", "", text, flags=re.IGNORECASE)
                failed = bool(re.search(r"\b(?:fail(?:ed|ure)?|lost|loss)\b", no_loss_removed, re.IGNORECASE))
                passed = bool(re.search(r"\b(?:pass(?:ed|es)?|success(?:ful(?:ly)?)?|succeed(?:ed|s)?)\b|\bno\s+data\s+loss\b", text, re.IGNORECASE))
                if passed or failed:
                    excerpt = next((line.strip() for line in text.splitlines()[1:] if line.strip()), "QA result recorded")
                    scoped.append((issue, group_id, passed, failed, excerpt))
                    refs.append(_evidence_ref(issue, f"{group_id} target-scope QA result: {excerpt}"))

        any_pass = any(passed for _, _, passed, _, _ in scoped)
        any_fail = any(failed for _, _, _, failed, _ in scoped)
        has_supersession = any(re.search(r"\bsupersed\w*\b", c, re.IGNORECASE) for c in qa_text)
        if any_pass and any_fail and not has_supersession:
            details = "; ".join(f"{issue.get('source_id')} ({group_id}): {excerpt}" for issue, group_id, p, f, excerpt in scoped if p or f)
            result["decision"] = "INSUFFICIENT_EVIDENCE"
            result["explanation"] = "Linked QA records for the target rollback configuration conflict: " + details + ". Neither explicitly supersedes or resolves the other; the manifest requires INSUFFICIENT_EVIDENCE."
            result["evidence_refs"] = [ref for ref in refs if ref]
            notes.append(f"{source_id}: evidence guard changed PASS to INSUFFICIENT_EVIDENCE because linked target-scope QA results conflict")
        elif not scoped:
            out_of_scope = []
            for issue in evidence:
                for group_id, text in _qa_result_groups(issue):
                    detail = "; ".join(_scope_mismatches_text(text, target, require_prompt=False))
                    out_of_scope.append(f"{issue.get('source_id')} ({group_id}): {detail}")
            details = "; ".join(out_of_scope)
            result["decision"] = "INSUFFICIENT_EVIDENCE"
            result["explanation"] = "No directly linked QA rehearsal provides an in-scope result for the target configuration. " + (details if details else "No directly linked QA evidence was available.")
            result["evidence_refs"] = [ref for ref in refs if ref]
            notes.append(f"{source_id}: evidence guard changed PASS to INSUFFICIENT_EVIDENCE because no target-scope QA rehearsal was verified")
        elif any_fail and not any_pass and not has_supersession:
            details = "; ".join(f"{issue.get('source_id')} ({group_id}): {excerpt}" for issue, group_id, _, failed, excerpt in scoped if failed)
            result["decision"] = "FAIL"
            result["explanation"] = "Applicable target-scope QA evidence reports a failed rollback rehearsal: " + details
            result["evidence_refs"] = [ref for ref in refs if ref]
            notes.append(f"{source_id}: evidence guard changed PASS to FAIL based on linked target-scope QA failure")

def normalize_and_validate(report: dict, backend, trace: list | None = None) -> dict:
    """Deterministic post-processing pass -- do not trust the model's own
    bookkeeping for things we can check or compute ourselves:
      - Map any requirement_key / evidence_ref key the model gave as a raw
        Jira issue key (e.g. "TYN-352") back to the record's real source_id
        (e.g. "REQ-05"), using the authoritative source_id -> key mapping
        from get_requirements()/get_issue() rather than guessing from the
        text.
      - Recompute overall_decision from the individual decisions per the
        stated rule (BLOCKED if any FAIL, else NEEDS_REVIEW if any
        INSUFFICIENT_EVIDENCE, else READY), rather than trusting whatever
        the model wrote for the aggregate.
      - Note (without crashing) if requirements are missing, duplicated, or
        carry a decision outside the allowed enum, so that's visible in the
        report rather than silently wrong.
    It also runs deterministic evidence guards for prompt-quality and rollback
    requirements. Those guards can downgrade a model PASS when linked QA
    records fail target-scope checks or contain unresolved target-scope
    contradictions. Any supplemental Jira reads are added to the trace.
    """
    if "requirements" not in report:
        return report  # a parse failure already recorded its own error; nothing to normalize

    try:
        known_requirements = backend.get_requirements()
    except Exception:  # noqa: BLE001 - normalization is best-effort, never fatal
        known_requirements = []

    key_to_source_id = {
        r["key"]: r.get("source_id") for r in known_requirements if isinstance(r, dict) and "key" in r
    }
    if trace is not None:
        for entry in trace:
            issue = entry.get("result")
            if entry.get("tool") == "get_issue" and isinstance(issue, dict):
                if issue.get("key") and issue.get("source_id"):
                    key_to_source_id[issue["key"]] = issue["source_id"]

    validation_notes = []
    seen_source_ids = []
    valid_decisions = {"PASS", "FAIL", "INSUFFICIENT_EVIDENCE"}

    for req in report["requirements"]:
        raw_key = req.get("requirement_key")
        if raw_key in key_to_source_id and key_to_source_id[raw_key]:
            normalized_key = key_to_source_id[raw_key]
            if raw_key != normalized_key:
                validation_notes.append(f"Normalized requirement_key {raw_key} -> {normalized_key}")
            req["requirement_key"] = normalized_key

        for ref in req.get("evidence_refs", []):
            ref_key = ref.get("key")
            if ref_key in key_to_source_id and key_to_source_id[ref_key]:
                ref["key"] = key_to_source_id[ref_key]

        seen_source_ids.append(req.get("requirement_key"))
        if req.get("decision") not in valid_decisions:
            validation_notes.append(
                f"{req.get('requirement_key')}: decision '{req.get('decision')}' is not a valid "
                f"enum value ({sorted(valid_decisions)})"
            )

    expected_count = len(known_requirements) if known_requirements else len(set(seen_source_ids))
    if expected_count and len(seen_source_ids) != expected_count:
        validation_notes.append(
            f"Expected {expected_count} requirements, report has {len(seen_source_ids)}"
        )
    duplicates = {rid for rid in seen_source_ids if seen_source_ids.count(rid) > 1}
    if duplicates:
        validation_notes.append(f"Duplicate requirement_key(s) in report: {sorted(duplicates)}")

    if trace is not None:
        _apply_evidence_consistency_guards(report, backend, known_requirements, trace, validation_notes)

    decisions = [req.get("decision") for req in report["requirements"]]
    if any(d == "FAIL" for d in decisions):
        computed_overall = "BLOCKED"
    elif any(d == "INSUFFICIENT_EVIDENCE" for d in decisions):
        computed_overall = "NEEDS_REVIEW"
    elif decisions and all(d == "PASS" for d in decisions):
        computed_overall = "READY"
    else:
        computed_overall = report.get("overall_decision", "UNKNOWN")

    if report.get("overall_decision") != computed_overall:
        validation_notes.append(
            f"overall_decision corrected from '{report.get('overall_decision')}' to "
            f"'{computed_overall}' (recomputed deterministically from the individual decisions)"
        )
    report["overall_decision"] = computed_overall

    if validation_notes:
        existing = report.get("notes", "") or ""
        joined = " | ".join(validation_notes)
        report["notes"] = f"{existing} [validator] {joined}".strip()

    return report


def render_markdown(report: dict) -> str:
    if "error" in report and "overall_decision" not in report:
        return f"# Release Readiness Report\n\n**Run failed:** {report['error']}\n"

    lines = ["# Release Readiness Report", ""]
    target = report.get("target", {})
    lines.append(
        f"**Target build:** release `{target.get('release')}`, commit `{target.get('commit')}`, "
        f"prompt `{target.get('prompt')}`, environment `{target.get('environment')}`, "
        f"service `{target.get('service')}`"
    )
    lines.append("")
    lines.append(f"## Overall decision: **{report.get('overall_decision', 'UNKNOWN')}**")
    lines.append("")
    lines.append("## Requirement-by-requirement decisions")
    lines.append("")
    for req in report.get("requirements", []):
        lines.append(f"### {req.get('requirement_key')} — {req.get('requirement_summary')}")
        lines.append(f"**Decision:** {req.get('decision')}")
        lines.append("")
        lines.append(req.get("explanation", ""))
        lines.append("")
        refs = req.get("evidence_refs", [])
        if refs:
            lines.append("**Evidence referenced:**")
            for ref in refs:
                lines.append(f"- `{ref.get('key')}` — {ref.get('note')}")
        lines.append("")
    if report.get("notes"):
        lines.append("## Notes / known issues encountered during the run")
        lines.append(report["notes"])
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["jira", "mock"], default="jira")
    parser.add_argument("--dataset", default="inputs/dataset.json", help="only used by --backend mock")
    parser.add_argument(
        "--provider",
        choices=["anthropic", "gemini"],
        default=os.environ.get("MODEL_PROVIDER", "anthropic"),
        help="which model provider drives the agent's tool-use loop",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="model name; defaults to ANTHROPIC_MODEL or GEMINI_MODEL env var depending on --provider",
    )
    args = parser.parse_args()

    backend = make_backend(args)

    if args.provider == "gemini":
        from agent.gemini_agent import run_agent_gemini

        model = args.model or os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
        final_text, trace, usage, elapsed = run_agent_gemini(backend, model)
    else:
        model = args.model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        final_text, trace, usage, elapsed = run_agent(backend, model)

    report = parse_final_json(final_text)
    report = normalize_and_validate(report, backend, trace)

    os.makedirs("outputs", exist_ok=True)

    with open("outputs/investigation_trace.jsonl", "w", encoding="utf-8") as f:
        for entry in trace:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    with open("outputs/release_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    with open("outputs/release_report.md", "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    with open("outputs/run_stats.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "provider": args.provider,
                "model": model,
                "backend": args.backend,
                "elapsed_seconds": round(elapsed, 2),
                "tool_calls": len(trace),
                "token_usage": usage,
            },
            f,
            indent=2,
        )

    print(f"Done in {elapsed:.1f}s, {len(trace)} tool calls, tokens={usage}")
    print("See outputs/release_report.md, outputs/investigation_trace.jsonl, outputs/run_stats.json")


if __name__ == "__main__":
    main()
