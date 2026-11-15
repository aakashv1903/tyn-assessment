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
- **Iteration budget.** The Anthropic loop caps at 40 round-trips
  (`max_iterations` in `agent.py`); the Gemini path caps its outer
  "keep investigating" loop at 8 rounds (`MAX_OUTER_ROUNDS` in
  `gemini_agent.py`), on top of whatever internal tool-call budget Google's
  SDK applies within each round. If a run hits its cap without producing a
  parseable final report, `outputs/release_report.json` records that
  explicitly rather than silently returning an empty report.
- **Gemini's automatic function calling has an internal per-call iteration
  cap that isn't independently configurable** in the (now-deprecated)
  `google-generativeai` SDK used here. A first version of this agent called
  `chat.send_message()` once per run and trusted that internal loop to
  explore the whole link graph; in testing it stopped early, having opened
  roughly half of the reachable evidence for the dataset's 44-record linked
  component, which produced several wrong decisions (see below). The fix
  applied here drives Gemini in an explicit outer loop (`run_agent_gemini`
  in `gemini_agent.py`) that checks whether a parseable final report has
  actually been produced and, if not, sends an explicit instruction to keep
  opening unvisited linked records before concluding. This is a pragmatic
  fix given the deprecated SDK's limited controls, not a guarantee of
  exhaustive traversal on every run -- if the model still concludes early,
  `outputs/investigation_trace.jsonl` will show fewer `get_issue` calls than
  the import has linked records reachable from the requirements, which is
  the signal to re-run or switch to `--provider anthropic`.
- **Ticket content as an injection vector.** The system prompt instructs the
  agent to treat ticket/comment text as evidence to evaluate, never as
  instructions to follow, and gives a concrete example matching the
  dataset's own cost-waiver comment. An earlier run still followed an
  instruction-shaped comment and marked a failing cost measurement as PASS;
  the prompt was strengthened with an explicit, concrete counter-example
  after that failure was caught. This is a real limitation of prompting
  alone as a defense -- a production version of this agent would also want
  a deterministic check that a PASS/FAIL claim is actually backed by a
  measured threshold comparison the code can verify, not just the model's
  stated reasoning.
- **`search_issues`'s JQL is NOT passed through to Jira as free text.**
  Both `agent/jira_tools.py` and `agent/mock_tools.py` parse the model's JQL
  fragment against a narrow allow-listed grammar (`labels = "value"`
  clauses ANDed together -- the only shape the system prompt ever asks the
  model to write) and construct the actual scoped query themselves; a
  fragment outside that grammar is rejected with an error rather than
  interpolated into a query string. `get_issue` additionally checks both
  the dataset label AND the Jira project key before returning an issue, as
  defense in depth. An earlier version wrapped the model's raw fragment in
  parentheses and trusted that to contain it, which doesn't reliably
  prevent a crafted fragment (e.g. one using unbalanced parentheses or an
  `OR`) from escaping the intended project/label scope; this version
  doesn't pass the fragment to Jira's query parser at all.
- **`outputs/release_report.json`'s `overall_decision` and identifiers are
  deterministically checked, not just trusted from the model's output.**
  `normalize_and_validate()` in `agent.py` recomputes `overall_decision`
  from the individual requirement decisions per the stated rule, and maps
  any requirement_key/evidence_ref the model gave as a raw Jira issue key
  (e.g. `TYN-352`) back to the record's real source_id (e.g. `REQ-05`)
  using the authoritative mapping from `get_requirements()`. Any
  correction it makes, or any structural problem it finds (a missing,
  duplicated, or invalid-enum requirement), is appended to the report's
  own `notes` field rather than silently fixed with no trace.
- **Gemini token-usage figures in `outputs/run_stats.json` are an
  approximation**, not an exact count. They sum `usage_metadata` across
  each outer-loop round's `send_message()` call; whether that figure fully
  reflects every underlying model call made inside Gemini's automatic
  function-calling loop for that round is not documented by the SDK.
  Treat it as directionally useful, not a billing-accurate count.
- I was not able to execute an end-to-end run myself while building the
  original version of this (no network access in the environment I built
  it in), so the first `--backend jira` / `--provider gemini` run (done by
  the person I built this for) was the first live test of the Jira-facing
  and Gemini-facing code. That run surfaced the real bugs described above
  (the model name needing updating twice as Gemini's lineup moved on, the
  free-tier daily quota on the flagship model, and -- caught by an
  independent review of the actual generated report against the dataset --
  the traversal and injection issues). They're described here rather than
  smoothed over, since that's a more honest account of how this was built
  and fixed than pretending it was correct on the first live run.

## AI assistance

This repository (importer, tool backends, system prompt, agent loop, and
these notes) was built with Claude's help, directed at Claude's existing
model/API knowledge and the assessment brief and dataset.json as the specs;
the dataset's own manifest text was used as the primary source for the
decision rubric encoded in `system_prompt.py`, rather than re-deriving it by
hand-inspecting individual records.

After the first real `--backend jira --provider gemini` run produced
`outputs/release_report.json`, a second AI tool (ChatGPT/Codex) was used to
independently re-derive a PASS/FAIL/INSUFFICIENT_EVIDENCE decision for each
of the 12 requirements directly from `inputs/dataset.json` -- without
looking at the generated report first -- and then diff that against what
the agent actually produced, plus do a separate code review of the four
files above. That review found four requirements where the agent's
Gemini-driven run reached the wrong decision (it stopped investigating a
link chain early in each case, and in one case followed an instruction
embedded in a ticket comment rather than the underlying measured result),
plus the JQL-scoping and output-validation issues described in Known
limits. The fixes described in this file were made in response to that
review, and the report in this submission is from the corrected code, not
the original run that review was performed against.
