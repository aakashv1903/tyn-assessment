# TYN Jira Release Evidence Agent

An AI agent that determines release readiness for `atlas-assistant` R2.4 by
investigating a dedicated Jira Cloud project through tool calls — it never
sees the raw dataset at inference time, only what it chooses to fetch from
Jira.

## What's here

```
importer/importer.py     # imports inputs/dataset.json into your Jira Cloud project
agent/jira_tools.py       # live Jira tool backend (the real evidence source)
agent/mock_tools.py       # offline dev-only stand-in, same interface, no Jira needed
agent/system_prompt.py    # the rubric the agent applies (see DESIGN.md)
agent/agent.py            # the tool-use loop, trace logging, report generation
inputs/dataset.json       # the supplied assessment dataset
outputs/                  # written by importer.py and agent.py when you run them
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values below
```

You need:
1. **A Jira Cloud site + project.** If you don't have one, create a free
   site at https://www.atlassian.com/software/jira/free and a project in it
   (any template — the importer only needs a "Task" issue type and the
   default "Relates" link type, both present by default). Put the project
   key in `JIRA_PROJECT_KEY`.
2. **A Jira API token**: https://id.atlassian.com/manage-profile/security/api-tokens
   → put it, your Jira account email, and your site URL in `.env`.
3. **A model provider API key** — pick one (the assessment brief explicitly allows
   choosing your own model provider):
   - **Gemini (free, no card required):** get a key at
     https://aistudio.google.com/apikey, put it in `GOOGLE_API_KEY`, and set
     `MODEL_PROVIDER=gemini` in `.env`.
   - **Anthropic (requires paid API credit):** get a key at
     https://console.anthropic.com, put it in `ANTHROPIC_API_KEY`, and set
     `MODEL_PROVIDER=anthropic` in `.env`.

## Run it

```bash
# 1. Import the dataset into your Jira project
python -m importer.importer --project YOUR_PROJECT_KEY

# 2. Run the agent against that live Jira project (provider read from .env,
#    or override explicitly with --provider gemini / --provider anthropic)
python -m agent.agent --backend jira
```

This writes:
- `outputs/release_report.md` — the human-readable release-readiness report
- `outputs/release_report.json` — the same, structured
- `outputs/investigation_trace.jsonl` — every tool call and result, in order
- `outputs/run_stats.json` — wall-clock time and token usage
- `outputs/import_map.json` — source_id → Jira issue key mapping (written by the importer)

The importer is safe to re-run (it looks up each record by its `src-<id>`
label before creating a duplicate).

### Optional: offline dry run

`python -m agent.agent --backend mock --dataset inputs/dataset.json` runs
the exact same agent and tool-call loop against the dataset directly in
memory, with no Jira account needed — useful to sanity-check reasoning
before spending real Jira/API calls, and reproduced in `outputs/` the same
way. This is a development aid only; the dataset_label/project scoping and
investigation trace it produces are not a substitute for the graded run,
which must come from the live Jira import (`--backend jira`).

## Design notes, limits, and AI assistance

See `DESIGN.md`.
