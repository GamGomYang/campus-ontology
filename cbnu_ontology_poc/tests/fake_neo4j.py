from __future__ import annotations

import re
from typing import Any, Mapping

from src.ontology_schema import NODE_KEY_MAP, NodeLabel, RelType


class FakeResult:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self._records = records or []

    def single(self) -> dict[str, Any] | None:
        if not self._records:
            return None
        return self._records[0]

    def data(self) -> list[dict[str, Any]]:
        return self._records


class FakeNeo4jClient:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, dict[str, Any]]] = {}
        self.relationships: list[tuple[str, str, str, str, str]] = []

    def run(
        self,
        query: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> FakeResult:
        params = dict(parameters or {})
        normalized = " ".join(query.split())

        if normalized == "MATCH (n) DETACH DELETE n":
            self.nodes.clear()
            self.relationships.clear()
            return FakeResult()

        if normalized.startswith("CREATE CONSTRAINT"):
            return FakeResult()

        if "UNWIND $rows AS row" in normalized and "MERGE (n:" in normalized:
            return self._load_nodes(normalized, params)

        if "MERGE (from)-[:" in normalized:
            return self._load_relationship(normalized, params)

        if normalized.startswith("MATCH (n:") and "RETURN count(n) AS count" in normalized:
            return self._count_nodes(normalized)

        if "RETURN count(*) AS count" in normalized:
            return self._count_relationships(normalized)

        if "MATCH (s:Student {student_id: $student_id})" in normalized:
            return self._student_context(params.get("student_id"))

        if "MATCH (c:Course {course_id: $course_id})" in normalized:
            return self._course_resources(params.get("course_id"))

        return FakeResult()

    def _load_nodes(self, query: str, params: dict[str, Any]) -> FakeResult:
        match = re.search(r"MERGE \(n:(\w+) \{(\w+): row\.\2\}\)", query)
        if not match:
            raise ValueError(f"Unsupported node load query: {query}")

        label, key = match.groups()
        bucket = self.nodes.setdefault(label, {})
        for row in params.get("rows", []):
            node = dict(row)
            bucket[str(node[key])] = node
        return FakeResult()

    def _load_relationship(self, query: str, params: dict[str, Any]) -> FakeResult:
        match = re.search(
            r"MATCH \(from:(\w+) \{(\w+): \$from_id\}\) "
            r"MATCH \(to:(\w+) \{(\w+): \$to_id\}\) "
            r"MERGE \(from\)-\[:(\w+)\]->\(to\)",
            query,
        )
        if not match:
            raise ValueError(f"Unsupported relationship load query: {query}")

        from_label, _from_key, to_label, _to_key, rel_type = match.groups()
        rel = (
            from_label,
            str(params["from_id"]),
            rel_type,
            to_label,
            str(params["to_id"]),
        )
        if rel not in self.relationships:
            self.relationships.append(rel)
        return FakeResult()

    def _count_nodes(self, query: str) -> FakeResult:
        match = re.search(r"MATCH \(n:(\w+)\) RETURN count\(n\) AS count", query)
        if not match:
            raise ValueError(f"Unsupported count query: {query}")
        label = match.group(1)
        return FakeResult([{"count": len(self.nodes.get(label, {}))}])

    def _count_relationships(self, query: str) -> FakeResult:
        match = re.search(
            r"MATCH \(s:(\w+) \{(\w+): '([^']+)'\}\)"
            r"-\[:(\w+)\]->"
            r"\(c:(\w+) \{(\w+): '([^']+)'\}\) "
            r"RETURN count\(\*\) AS count",
            query,
        )
        if not match:
            raise ValueError(f"Unsupported relationship count query: {query}")

        from_label, _from_key, from_id, rel_type, to_label, _to_key, to_id = match.groups()
        count = sum(
            1
            for rel in self.relationships
            if rel == (from_label, str(from_id), rel_type, to_label, str(to_id))
        )
        return FakeResult([{"count": count}])

    def _student_context(self, student_id: Any) -> FakeResult:
        student = self._node(NodeLabel.STUDENT, student_id)
        if student is None:
            return FakeResult()

        courses = self._targets(NodeLabel.STUDENT, student_id, RelType.ENROLLED_IN, NodeLabel.COURSE)
        books = self._course_targets(courses, RelType.USES_BOOK, NodeLabel.BOOK)
        programs = self._course_targets(courses, RelType.RELATED_PROGRAM, NodeLabel.PROGRAM)
        scholarships = self._sources(NodeLabel.SCHOLARSHIP, RelType.REQUIRES_COURSE, courses, NodeLabel.COURSE)

        return FakeResult(
            [
                {
                    "s": student,
                    "courses": courses,
                    "books": books,
                    "programs": programs,
                    "scholarships": scholarships,
                }
            ]
        )

    def _course_resources(self, course_id: Any) -> FakeResult:
        course = self._node(NodeLabel.COURSE, course_id)
        if course is None:
            return FakeResult()

        courses = [course]
        books = self._course_targets(courses, RelType.USES_BOOK, NodeLabel.BOOK)
        programs = self._course_targets(courses, RelType.RELATED_PROGRAM, NodeLabel.PROGRAM)
        scholarships = self._sources(NodeLabel.SCHOLARSHIP, RelType.REQUIRES_COURSE, courses, NodeLabel.COURSE)

        return FakeResult(
            [
                {
                    "c": course,
                    "books": books,
                    "programs": programs,
                    "scholarships": scholarships,
                }
            ]
        )

    def _node(self, label: NodeLabel, node_id: Any) -> dict[str, Any] | None:
        return self.nodes.get(label.value, {}).get(str(node_id))

    def _node_id(self, label: NodeLabel, node: dict[str, Any]) -> str:
        return str(node[NODE_KEY_MAP[label]])

    def _targets(
        self,
        from_label: NodeLabel,
        from_id: Any,
        rel_type: RelType,
        to_label: NodeLabel,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rel in self.relationships:
            if rel[:3] != (from_label.value, str(from_id), rel_type.value):
                continue
            if rel[3] != to_label.value or rel[4] in seen:
                continue
            node = self._node(to_label, rel[4])
            if node is not None:
                result.append(node)
                seen.add(rel[4])
        return result

    def _course_targets(
        self,
        courses: list[dict[str, Any]],
        rel_type: RelType,
        to_label: NodeLabel,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for course in courses:
            course_id = self._node_id(NodeLabel.COURSE, course)
            for node in self._targets(NodeLabel.COURSE, course_id, rel_type, to_label):
                node_id = self._node_id(to_label, node)
                if node_id in seen:
                    continue
                result.append(node)
                seen.add(node_id)
        return result

    def _sources(
        self,
        from_label: NodeLabel,
        rel_type: RelType,
        target_nodes: list[dict[str, Any]],
        target_label: NodeLabel,
    ) -> list[dict[str, Any]]:
        target_ids = {self._node_id(target_label, node) for node in target_nodes}
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_label, source_id, rel, to_label, to_id in self.relationships:
            if source_label != from_label.value or rel != rel_type.value:
                continue
            if to_label != target_label.value or to_id not in target_ids or source_id in seen:
                continue
            node = self._node(from_label, source_id)
            if node is not None:
                result.append(node)
                seen.add(source_id)
        return result
