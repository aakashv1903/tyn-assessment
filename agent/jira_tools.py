"""
Tool backend the agent calls into. Every method is scoped to the dataset
label AND project so the agent can never see tickets outside this import,
even if the Jira project contains other issues, and even if a ticket's own
text tries to talk the model into writing a JQL fragment that escapes the
intended scope.

This is the ONLY place that talks to Jira. The agent never receives the raw
dataset.json -- all evidence is fetched live, issue by issue, through these
tools, which is what makes this a genuine investigation rather than a
dump-the-dataset script.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from jira import JIRA

# The system prompt only ever asks the model to write JQL of the form
# `labels = "value"` ANDed together (see system_prompt.py's search_issues
# guidance). We do not interpolate the model's JQL fragment into a query
# string at all -- we parse out just the label values it named and build
# the query ourselves. Anything that doesn't match this narrow grammar is
# rejected rather than silently passed through or silently ignored, so a
# fragment designed to escape the intended scope (e.g. unbalanced
# parentheses, an OR clause, a second `project = `) simply cannot reach
# Jira's query parser.
_LABEL_CLAUSE = re.compile(r'labels\s*=\s*"([^"]*)"')
_ALLOWED_JQL_SHAPE = re.compile(
    r'^\s*(labels\s*=\s*"[^"]*"\s*(AND\s*)?)*\s*$', re.IGNORECASE
)


def _extract_allowed_labels(jql: str):
    """Returns (label_values, error). error is set if the fragment contains
    anything outside the narrow `labels = "..." AND ...` grammar we support."""
    fragment = jql.strip()
    if not fragment:
        return [], None
    if not _ALLOWED_JQL_SHAPE.match(fragment):
        return [], (
            'Unsupported JQL. Only `labels = "value"` clauses ANDed together '
            'are supported, e.g. labels = "kind-work" AND labels = "release-R2.4".'
        )
    return _LABEL_CLAUSE.findall(fragment), None


@dataclass
class JiraToolBackend:
    jira: JIRA
    project_key: str
    dataset_label: str

    @classmethod
    def from_env(cls) -> "JiraToolBackend":
        jira = JIRA(
            server=os.environ["JIRA_SERVER"],
            basic_auth=(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"]),
        )
        return cls(
            jira=jira,
            project_key=os.environ["JIRA_PROJECT_KEY"],
            dataset_label=os.environ.get("DATASET_LABEL", "tyn-release-assessment-v2"),
        )

    @staticmethod
    def _source_id(labels) -> Optional[str]:
        for label in labels or []:
            if label.startswith("src-"):
                return label[len("src-") :]
        return None

    def search_issues(self, jql: str, max_results: int = 50) -> list:
        extra_labels, error = _extract_allowed_labels(jql)
        if error:
            return [{"error": error}]

        clauses = [f'project = "{self.project_key}"', f'labels = "{self.dataset_label}"']
        clauses += [f'labels = "{label}"' for label in extra_labels]
        scoped = " AND ".join(clauses)

        issues = self.jira.search_issues(
            scoped, maxResults=max_results, fields="summary,status,labels"
        )
        return [
            {
                "key": i.key,
                "source_id": self._source_id(i.fields.labels),
                "summary": i.fields.summary,
                "status": i.fields.status.name,
                "labels": i.fields.labels,
            }
            for i in issues
        ]

    def get_issue(self, key: str) -> dict:
        issue = self.jira.issue(
            key, fields="summary,description,status,labels,comment,issuelinks,project"
        )

        # Defensive scope check: refuse to return an issue that isn't part
        # of THIS project's THIS import, in case the model (possibly
        # steered by text inside a ticket) tries to reach a key outside
        # scope. Both checks matter: the label alone isn't enough if the
        # same label string were ever reused in another project.
        in_project = issue.fields.project.key == self.project_key
        has_label = self.dataset_label in (issue.fields.labels or [])
        if not (in_project and has_label):
            return {"error": f"{key} is outside the {self.dataset_label} import scope."}

        comments = [c.body for c in issue.fields.comment.comments] if issue.fields.comment else []

        links = []
        for link in issue.fields.issuelinks or []:
            other = getattr(link, "outwardIssue", None) or getattr(link, "inwardIssue", None)
            direction = "outward" if hasattr(link, "outwardIssue") else "inward"
            if other is not None:
                links.append(
                    {
                        "key": other.key,
                        "summary": other.fields.summary,
                        "direction": direction,
                        "link_type": link.type.name,
                    }
                )

        return {
            "key": issue.key,
            "source_id": self._source_id(issue.fields.labels),
            "summary": issue.fields.summary,
            "description": issue.fields.description,
            "status": issue.fields.status.name,
            "labels": issue.fields.labels,
            "comments": comments,
            "links": links,
        }

    def get_requirements(self) -> list:
        """Convenience tool: the manifest-linked requirement records for
        this import (label kind-requirement), each with key + summary so
        the model can decide which to open first."""
        return self.search_issues('labels = "kind-requirement"', max_results=50)

    def get_manifest(self) -> dict:
        results = self.search_issues('labels = "kind-manifest"', max_results=1)
        if not results:
            return {"error": "No manifest record found in this import scope."}
        return self.get_issue(results[0]["key"])
