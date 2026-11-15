"""
Offline stand-in for JiraToolBackend, backed directly by inputs/dataset.json.

This exists ONLY so you can dry-run and debug the agent's reasoning and tool
use without spending real Jira/Anthropic calls, before (or instead of,
during development) pointing it at a live Jira site. It implements the exact
same method signatures as JiraToolBackend so agent.py is unaware which one it
is talking to.

IMPORTANT: this is a development aid, not a substitute for the real import.
The assessment specifically requires evidence to be gathered from Jira, so
the graded run must use --backend jira.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class MockToolBackend:
    dataset_label: str
    records_by_id: dict = field(default_factory=dict)

    @classmethod
    def from_dataset(cls, path: str) -> "MockToolBackend":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        records_by_id = {r["source_id"]: r for r in data["records"]}
        return cls(dataset_label=data["dataset_label"], records_by_id=records_by_id)

    def _matches(self, record: dict, jql_fragment: str) -> bool:
        """Extremely small JQL-ish matcher: only understands
        `labels = "..."` clauses ANDed together, which is all this agent's
        prompt ever asks the model to write. Good enough for a local dry run."""
        fragment = jql_fragment.strip()
        if not fragment:
            return True
        labels = {
            "kind-" + record["kind"],
            "release-" + str(record.get("release", "none")),
            "src-" + record["source_id"],
            self.dataset_label,
        }
        import re

        wanted = re.findall(r'labels\s*=\s*"([^"]+)"', fragment)
        return all(w in labels for w in wanted) if wanted else True

    def search_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        out = []
        for r in self.records_by_id.values():
            if self._matches(r, jql):
                out.append(
                    {
                        "key": r["source_id"],
                        "summary": r["summary"],
                        "status": r["recorded_status"],
                        "labels": [
                            self.dataset_label,
                            f"kind-{r['kind']}",
                            f"release-{r.get('release', 'none')}",
                            f"src-{r['source_id']}",
                        ],
                    }
                )
            if len(out) >= max_results:
                break
        return out

    def get_issue(self, key: str) -> dict:
        r = self.records_by_id.get(key)
        if not r:
            return {"error": f"{key} is outside the {self.dataset_label} import scope."}
        links = []
        for linked_id in r.get("links", []):
            other = self.records_by_id.get(linked_id)
            if other:
                links.append(
                    {
                        "key": other["source_id"],
                        "summary": other["summary"],
                        "direction": "outward",
                        "link_type": "Relates",
                    }
                )
        return {
            "key": r["source_id"],
            "summary": r["summary"],
            "description": r.get("description", ""),
            "status": r["recorded_status"],
            "labels": [f"kind-{r['kind']}"],
            "comments": r.get("comments", []),
            "links": links,
        }

    def get_requirements(self) -> list[dict]:
        return self.search_issues('labels = "kind-requirement"', max_results=50)

    def get_manifest(self) -> dict:
        results = self.search_issues('labels = "kind-manifest"', max_results=1)
        if not results:
            return {"error": "No manifest record found in this import scope."}
        return self.get_issue(results[0]["key"])
