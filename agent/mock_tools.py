"""
Offline stand-in for JiraToolBackend, backed directly by inputs/dataset.json.

This exists ONLY so you can dry-run and debug the agent's reasoning and tool
use without spending real Jira/Anthropic/Gemini calls, before (or instead
of, during development) pointing it at a live Jira site. It implements the
exact same method signatures as JiraToolBackend so agent.py is unaware which
one it is talking to.

Mirrors two things the real Jira backend does, both load-bearing for
correctness, not just style:
  - Links are treated as BIDIRECTIONAL, the same way a Jira "Relates" link
    is visible from either issue once created. The dataset only lists each
    link in one direction (the side it was written from); if we only
    followed that direction here, a requirement linked only incoming from
    some evidence record would look unlinked, which is not how the real
    import behaves.
  - search_issues only understands the same narrow `labels = "value" AND
    ...` grammar the real backend accepts; anything outside that shape is
    rejected rather than silently treated as "match everything", so a
    JQL mistake looks the same way here as it would against live Jira.

IMPORTANT: this is a development aid, not a substitute for the real import.
The assessment specifically requires evidence to be gathered from Jira, so
the graded run must use --backend jira.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

_LABEL_CLAUSE = re.compile(r'labels\s*=\s*"([^"]*)"')
_ALLOWED_JQL_SHAPE = re.compile(
    r'^\s*(labels\s*=\s*"[^"]*"\s*(AND\s*)?)*\s*$', re.IGNORECASE
)


def _extract_allowed_labels(jql: str):
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
class MockToolBackend:
    dataset_label: str
    records_by_id: dict = field(default_factory=dict)
    # source_id -> set of linked source_ids, BOTH directions merged in.
    link_graph: dict = field(default_factory=dict)

    @classmethod
    def from_dataset(cls, path: str) -> "MockToolBackend":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        records_by_id = {r["source_id"]: r for r in data["records"]}

        link_graph: dict = {rid: set() for rid in records_by_id}
        for r in records_by_id.values():
            for linked_id in r.get("links", []):
                if linked_id not in records_by_id:
                    continue
                link_graph[r["source_id"]].add(linked_id)
                link_graph[linked_id].add(r["source_id"])  # mirror, like a Jira Relates link

        return cls(dataset_label=data["dataset_label"], records_by_id=records_by_id, link_graph=link_graph)

    def _labels_for(self, record: dict) -> list:
        return [
            self.dataset_label,
            f"kind-{record['kind']}",
            f"release-{record.get('release', 'none')}",
            f"src-{record['source_id']}",
        ]

    def _record_matches(self, record: dict, wanted_labels: list) -> bool:
        have = set(self._labels_for(record))
        return all(w in have for w in wanted_labels)

    def search_issues(self, jql: str, max_results: int = 50) -> list:
        wanted_labels, error = _extract_allowed_labels(jql)
        if error:
            return [{"error": error}]

        out = []
        for r in self.records_by_id.values():
            if self._record_matches(r, wanted_labels):
                out.append(
                    {
                        "key": r["source_id"],
                        "source_id": r["source_id"],
                        "summary": r["summary"],
                        "status": r["recorded_status"],
                        "labels": self._labels_for(r),
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
        for linked_id in sorted(self.link_graph.get(key, set())):
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
            "source_id": r["source_id"],
            "summary": r["summary"],
            "description": r.get("description", ""),
            "status": r["recorded_status"],
            "labels": self._labels_for(r),
            "comments": r.get("comments", []),
            "links": links,
        }

    def get_requirements(self) -> list:
        return self.search_issues('labels = "kind-requirement"', max_results=50)

    def get_manifest(self) -> dict:
        results = self.search_issues('labels = "kind-manifest"', max_results=1)
        if not results:
            return {"error": "No manifest record found in this import scope."}
        return self.get_issue(results[0]["key"])
