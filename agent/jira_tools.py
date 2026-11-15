"""
Tool backend the agent calls into. Every method is scoped to the dataset
label so the agent can never see tickets outside this import, even if the
Jira project contains other issues.

This is the ONLY place that talks to Jira. The agent never receives the raw
dataset.json -- all evidence is fetched live, issue by issue, through these
tools, which is what makes this a genuine investigation rather than a
dump-the-dataset script.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jira import JIRA


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

    def _scope(self, jql: str) -> str:
        """Always AND in the project + dataset label scope, so the model
        cannot escape it even if it writes a broader JQL query itself."""
        base = f'project = "{self.project_key}" AND labels = "{self.dataset_label}"'
        return f"{base} AND ({jql})" if jql.strip() else base

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        scoped = self._scope(jql)
        issues = self.jira.search_issues(
            scoped, maxResults=max_results, fields="summary,status,labels"
        )
        return [
            {
                "key": i.key,
                "summary": i.fields.summary,
                "status": i.fields.status.name,
                "labels": i.fields.labels,
            }
            for i in issues
        ]

    def get_issue(self, key: str) -> dict:
        issue = self.jira.issue(key, fields="summary,description,status,labels,comment,issuelinks")

        # Defensive scope check: refuse to return an issue that isn't part
        # of this import, in case the model guesses a key directly.
        if self.dataset_label not in (issue.fields.labels or []):
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
            "summary": issue.fields.summary,
            "description": issue.fields.description,
            "status": issue.fields.status.name,
            "labels": issue.fields.labels,
            "comments": comments,
            "links": links,
        }

    def get_requirements(self) -> list[dict]:
        """Convenience tool: the manifest-linked requirement records for
        this import (label kind-requirement), each with key + summary so
        the model can decide which to open first."""
        return self.search_issues('labels = "kind-requirement"', max_results=50)

    def get_manifest(self) -> dict:
        results = self.search_issues('labels = "kind-manifest"', max_results=1)
        if not results:
            return {"error": "No manifest record found in this import scope."}
        return self.get_issue(results[0]["key"])
