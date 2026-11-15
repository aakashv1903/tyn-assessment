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
        "description": "List the requirement records (key + summary + status) for this import.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_issues",
        "description": (
            "Search issues within this import's scope using a JQL fragment, e.g. "
            '\'labels = "kind-work" AND labels = "release-R2.4"\'. '
            "Always scoped automatically to this dataset's project/label; you do not "
            "need to (and should not try to) add your own project/dataset-label clause."
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
            "Fetch one issue's full detail: summary, description, status, comments, and "
            "its linked issues (key + summary + link direction/type). Use this to read "
            "evidence and to discover further records to follow via `links`."
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

        model = args.model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        final_text, trace, usage, elapsed = run_agent_gemini(backend, model)
    else:
        model = args.model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        final_text, trace, usage, elapsed = run_agent(backend, model)

    report = parse_final_json(final_text)

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
