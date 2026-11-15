"""
Importer for the TYN Jira Release Evidence Agent assessment.

Reads inputs/dataset.json and creates one Jira issue per record in a target
Jira Cloud project, preserving:
  - source identifiers      -> label `src-<source_id>` + "[<source_id>]" in the summary
  - dataset scope           -> label `<dataset_label>` (from the dataset itself)
  - record kind             -> label `kind-<kind>` (evidence / work / requirement / manifest)
  - release                 -> label `release-<release>`
  - recorded evidence       -> the record's `comments` list, added verbatim as native
                               Jira comments (the comment TEXT already encodes the
                               fictional actor/role/timestamp per the assessment's
                               "Recorded event ..." convention -- the native Jira
                               comment author/timestamp is the importer's own and is
                               intentionally NOT presented as the scenario actor)
  - relationships           -> native Jira issue links, created in a second pass
                               once every issue exists and we have a full
                               source_id -> issue_key map

Run:
    python -m importer.importer --dataset inputs/dataset.json

Idempotency: safe to re-run. On each run we first search for existing issues
carrying the dataset's `src-<source_id>` label and skip creating a duplicate;
we also skip a link if it already exists between the two issues.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

from dotenv import load_dotenv
from jira import JIRA
from jira.exceptions import JIRAError

load_dotenv()

ISSUE_TYPE = "Task"
LINK_TYPE_NAME = "Relates"  # standard Jira Cloud link type present on every site


def jira_safe_label(raw: str) -> str:
    """Jira labels may not contain whitespace; dataset ids/kinds are already
    whitespace-free, but we defensively normalise anyway."""
    return raw.strip().replace(" ", "-")


@dataclass
class ImportMaps:
    source_to_key: dict
    created_this_run: set


def connect() -> JIRA:
    server = os.environ["JIRA_SERVER"]
    email = os.environ["JIRA_EMAIL"]
    token = os.environ["JIRA_API_TOKEN"]
    return JIRA(server=server, basic_auth=(email, token))


def find_existing_issue(jira: JIRA, project_key: str, src_label: str):
    jql = f'project = "{project_key}" AND labels = "{src_label}"'
    results = jira.search_issues(jql, maxResults=1, fields="comment")
    return results[0] if results else None


def reconcile_comments(jira: JIRA, issue, record: dict) -> None:
    """If a previous run created the issue but failed partway through adding
    its comments (e.g. a transient API error), a plain re-run would silently
    leave those comments missing forever, since find_existing_issue would
    just skip the issue entirely. Instead, top up any comments beyond what's
    already there. This assumes comments are only ever appended in the same
    order dataset-side, which holds for this dataset."""
    existing_count = len(issue.fields.comment.comments) if issue.fields.comment else 0
    wanted = record.get("comments", [])
    if existing_count >= len(wanted):
        return
    for comment in wanted[existing_count:]:
        try:
            jira.add_comment(issue, comment)
        except JIRAError as e:
            print(f"    (warning) could not backfill a comment on {issue.key}: {e}")


def create_record_issue(jira: JIRA, project_key: str, record: dict) -> str:
    source_id = record["source_id"]
    src_label = jira_safe_label(f"src-{source_id}")

    existing = find_existing_issue(jira, project_key, src_label)
    if existing is not None:
        reconcile_comments(jira, existing, record)
        print(f"  [skip] {source_id} already imported as {existing.key}")
        return existing.key

    labels = [
        jira_safe_label(record["_dataset_label"]),
        jira_safe_label(f"kind-{record['kind']}"),
        jira_safe_label(f"release-{record.get('release', 'none')}"),
        src_label,
    ]

    fields = {
        "project": {"key": project_key},
        "summary": f"[{source_id}] {record['summary']}",
        "description": record.get("description", ""),
        "issuetype": {"name": ISSUE_TYPE},
        "labels": labels,
    }

    issue = jira.create_issue(fields=fields)
    print(f"  [new]  {source_id} -> {issue.key}")

    # Match the dataset's recorded_status as closely as the project's default
    # workflow allows. Jira Cloud Task workflows commonly expose To Do / In
    # Progress / Done; if a transition isn't available on this site's
    # workflow we log and continue rather than fail the whole import.
    desired_status = record.get("recorded_status")
    if desired_status:
        try:
            transitions = {t["name"]: t["id"] for t in jira.transitions(issue)}
            if desired_status in transitions:
                jira.transition_issue(issue, transitions[desired_status])
        except JIRAError as e:
            print(f"    (warning) could not set status '{desired_status}' on {issue.key}: {e}")

    for comment in record.get("comments", []):
        try:
            jira.add_comment(issue, comment)
        except JIRAError as e:
            print(f"    (warning) could not add a comment on {issue.key}: {e}")

    return issue.key


def link_exists(jira: JIRA, key_a: str, key_b: str) -> bool:
    issue = jira.issue(key_a, fields="issuelinks")
    for link in issue.fields.issuelinks:
        other = getattr(link, "outwardIssue", None) or getattr(link, "inwardIssue", None)
        if other and other.key == key_b:
            return True
    return False


def create_links(jira: JIRA, records: list[dict], source_to_key: dict) -> None:
    for record in records:
        src_key = source_to_key.get(record["source_id"])
        if not src_key:
            continue
        for linked_source_id in record.get("links", []):
            dst_key = source_to_key.get(linked_source_id)
            if not dst_key:
                print(f"  (warning) {record['source_id']} links to unknown {linked_source_id}")
                continue
            try:
                if link_exists(jira, src_key, dst_key):
                    continue
                jira.create_issue_link(
                    type=LINK_TYPE_NAME, inwardIssue=src_key, outwardIssue=dst_key
                )
                print(f"  [link] {src_key} -> {dst_key}")
            except JIRAError as e:
                print(f"    (warning) could not link {src_key} -> {dst_key}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="inputs/dataset.json")
    parser.add_argument("--project", default=os.environ.get("JIRA_PROJECT_KEY"))
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="seconds to sleep between issue creations, to stay under rate limits",
    )
    args = parser.parse_args()

    if not args.project:
        sys.exit("No Jira project key given (--project or JIRA_PROJECT_KEY env var).")

    with open(args.dataset, encoding="utf-8") as f:
        dataset = json.load(f)

    dataset_label = dataset["dataset_label"]
    records = dataset["records"]
    for r in records:
        r["_dataset_label"] = dataset_label

    print(f"Dataset label: {dataset_label}")
    print(f"Target: {dataset['target']}")
    print(f"Records to import: {len(records)}")

    jira = connect()

    source_to_key = {}
    for i, record in enumerate(records, 1):
        key = create_record_issue(jira, args.project, record)
        source_to_key[record["source_id"]] = key
        if args.sleep:
            time.sleep(args.sleep)
        if i % 25 == 0:
            print(f"  ... {i}/{len(records)}")

    print("Linking records...")
    create_links(jira, records, source_to_key)

    os.makedirs("outputs", exist_ok=True)
    with open("outputs/import_map.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "dataset_label": dataset_label,
                "target": dataset["target"],
                "project": args.project,
                "source_to_key": source_to_key,
            },
            f,
            indent=2,
        )
    print("Wrote outputs/import_map.json")
    print("Done.")


if __name__ == "__main__":
    main()
