# Design notes

## Architecture

Two independent stages, connected only by Jira:

1. **Importer** (`importer/importer.py`) reads `inputs/dataset.json` once and
   writes it into a real Jira Cloud project: one issue per record, labelled
   with the dataset's own `dataset_label` plus `kind-<kind>`,
   `release-<release>`, and `src-<source_id>` for exact lookup, status
   transitioned to match the record's `recorded_status` where the project's
   workflow allows it, comments added verbatim (they already carry the
   fictional actor/role/timestamp as plain text per the assessment's
   "Recorded event ..." convention — the native Jira comment author and
   timestamp are the importer's own, not the scenario actor, by design).
   Links are created in a second pass once every source_id has a known Jira
   key, using Jira's native issue-link feature.

2. **Agent** (`agent/agent.py`) never reads `dataset.json`. It runs a normal
   Claude tool-use loop against four tools backed by live Jira JQL/REST
   calls (`agent/jira_tools.py`), every one of them hard-scoped to the
   dataset's label and the configured project, so the agent cannot see
   tickets outside this import even if the project has other issues in it.
   The system prompt (`agent/system_prompt.py`) tells it to open the
   manifest, list the 12 requirements, and then, requirement by requirement,
   fetch the linked records and follow further links as needed before
   deciding PASS / FAIL / INSUFFICIENT_EVIDENCE — mirroring the manifest's
   own written assessment rules so the rubric is enforced both by prompt
   and, loosely, by what the tools make easy to do. Every tool call and its
   result is appended to `outputs/investigation_trace.jsonl` as it happens,
   so a run that fails partway still leaves a readable trace of what was
   checked.

A `mock_tools.py` backend implements the same four methods directly against
the in-memory dataset, so the whole agent loop can be exercised without a
Jira account — this is strictly a development aid (see README); the graded
artifact is the `--backend jira` run.

## Why these tools, specifically

Four tools, each matching something the system prompt actually asks the
model to do: `get_manifest` and `get_requirements` as fixed entry points
(no reason to make the model guess JQL for things it always needs first),
`get_issue` for reading one record's comments/description/links, and
`search_issues` as a general-purpose JQL escape hatch for anything not
directly linked — e.g. finding a later retest that supersedes an earlier
QA run, which the dataset models as independent records rather than an
explicit "supersedes" link. Keeping the tool surface this small, rather
than exposing the whole Jira API, keeps the model's exploration close to
what the requirement actually needs instead of wandering.

## Model provider

The agent loop is implemented against two providers behind the same
tool backend and system prompt: Anthropic (`agent/agent.py`'s
`run_agent`, native tool-use) and Google Gemini (`agent/gemini_agent.py`,
using the Gemini SDK's automatic function calling, with the same four
tools and the same trace/report output format). `--provider` selects
between them; both write identical `outputs/*` artifacts. Gemini was
added specifically because the brief allows choosing your own model
provider and its AI Studio free tier needs no payment method, whereas
Anthropic's API requires prepaid credit.

## Known limits

- **Jira Cloud cannot backdate comment authorship or timestamps** through
  the standard REST API available to a normal site (that's a Data
  Center/Server migration-only capability), so the scenario's actor/role/
  time information is preserved as plain text inside each comment body
  rather than as native Jira metadata. The system prompt explicitly tells
  the agent to treat the recorded text, not the native Jira author/
  timestamp, as the fact of record.
- **Status transitions during import are best-effort.** Project workflows
  vary by Jira site/template; if a project's workflow doesn't expose a
  transition matching a record's `recorded_status`, the importer logs a
  warning and leaves the issue's default status rather than failing the
  whole import. This doesn't affect the agent, which is instructed never to
  treat status as evidence either way.
- **`search_issues`'s JQL is passed close to verbatim to Jira**, wrapped in
  an AND with the project/label scope; it does not otherwise sanitise the
  fragment. This is acceptable here because the only caller is the model
  under our own system prompt, not an untrusted end user.
- **Iteration budget.** The agent loop caps at 40 round-trips
  (`max_iterations` in `agent.py`) as a safety bound; if a run hits that
  cap without producing a final answer, `outputs/release_report.json`
  records that explicitly rather than silently returning an empty report.
- I was not able to execute an end-to-end run myself while building this
  (no network access in the environment I built it in), so the Jira
  REST/JQL call shapes are implemented against the documented `jira` Python
  client API rather than verified against a live site. The offline mock
  backend was used to verify the tool-call loop, trace logging, report
  parsing, and JSON rendering all work correctly end-to-end before this was
  handed over; the first real `--backend jira` run should be treated as the
  first live test of the Jira-facing half specifically.

## AI assistance

This repository (importer, tool backends, system prompt, agent loop, and
these notes) was built with Claude's help, directed at Claude's existing
model/API knowledge and the assessment brief and dataset.json as the specs;
the dataset's own manifest text was used as the primary source for the
decision rubric encoded in `system_prompt.py`, rather than re-deriving it by
hand-inspecting individual records.
