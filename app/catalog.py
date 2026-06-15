from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GENERIC_MATCH_VALUES = {
    "",
    "all",
    "archive",
    "chat-card",
    "conversation",
    "creative",
    "done",
    "ideas",
    "memory",
    "misc",
    "note",
    "notes",
    "notebooklm",
    "obsidian",
    "project",
    "projects",
    "reference",
    "summary",
    "text-export",
}
EXTRA_ALIASES: dict[str, set[str]] = {
    # Optional hand-written aliases that map an extra phrasing onto a canonical
    # taxonomy value, e.g. "myproject": {"my project", "my-project"}.
}


def slugify_value(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value


def query_text(value: str) -> str:
    return f" {slugify_value(value).replace('-', ' ')} "


def alias_variants(value: str) -> set[str]:
    variants = {value, value.replace("-", " ")}
    variants.update(EXTRA_ALIASES.get(value, set()))
    return {variant.strip().lower() for variant in variants if variant.strip()}


@dataclass
class CorpusRecord:
    import_record_id: str
    title: str
    text: str
    metadata: dict[str, Any]
    source_ref: str


@dataclass
class QueryRoute:
    filters: dict[str, Any] | None
    description: str
    matched: dict[str, str]


class CorpusCatalog:
    def __init__(self, records: list[CorpusRecord]):
        self.records_by_id = {record.import_record_id: record for record in records}
        self.alias_maps: dict[str, defaultdict[str, set[str]]] = {
            "domain": defaultdict(set),
            "hall": defaultdict(set),
            "room": defaultdict(set),
            "topic": defaultdict(set),
        }
        self.value_counts: dict[str, defaultdict[str, int]] = {
            "domain": defaultdict(int),
            "hall": defaultdict(int),
            "room": defaultdict(int),
            "topic": defaultdict(int),
        }
        self.domains_by_room: defaultdict[str, set[str]] = defaultdict(set)
        self.domains_by_topic: defaultdict[str, set[str]] = defaultdict(set)
        self.routeable_domains: list[str] = []
        self._build_indexes(records)

    @classmethod
    def from_path(cls, dataset_path: str) -> "CorpusCatalog":
        path = Path(dataset_path)
        paths: list[Path]
        if path.is_dir():
            paths = sorted(path.glob("*.jsonl"))
        else:
            paths = [path]

        records: list[CorpusRecord] = []
        for current_path in paths:
            with current_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    metadata = payload.get("metadata") or {}
                    records.append(
                        CorpusRecord(
                            import_record_id=str(payload["id"]),
                            title=str(metadata.get("title") or "<untitled>"),
                            text=str(payload.get("text") or ""),
                            metadata=metadata,
                            source_ref=str(metadata.get("source_ref") or ""),
                        )
                    )
        return cls(records)

    def _build_indexes(self, records: list[CorpusRecord]) -> None:
        domains: set[str] = set()
        for record in records:
            metadata = record.metadata
            for field in ("domain", "hall", "room", "topic"):
                value = slugify_value(str(metadata.get(field) or ""))
                if not value:
                    continue
                self.value_counts[field][value] += 1
                if field in {"hall", "room", "topic"} and value in GENERIC_MATCH_VALUES:
                    continue
                for alias in alias_variants(value):
                    self.alias_maps[field][alias].add(value)

            domain = slugify_value(str(metadata.get("domain") or ""))
            room = slugify_value(str(metadata.get("room") or ""))
            topic = slugify_value(str(metadata.get("topic") or ""))
            if domain:
                domains.add(domain)
            if domain and room:
                self.domains_by_room[room].add(domain)
            if domain and topic:
                self.domains_by_topic[topic].add(domain)

        self.routeable_domains = sorted(domain for domain in domains if domain not in GENERIC_MATCH_VALUES)

    def _match_field(self, field: str, normalized_query: str, *, domain: str | None = None) -> str | None:
        candidates: list[tuple[int, int, str]] = []
        for alias in sorted(self.alias_maps[field], key=len, reverse=True):
            if f" {alias.replace('-', ' ')} " not in normalized_query:
                continue
            values = set(self.alias_maps[field][alias])
            if domain and field == "room":
                values = {value for value in values if domain in self.domains_by_room[value]}
            if domain and field == "topic":
                values = {value for value in values if domain in self.domains_by_topic[value]}
            if len(values) != 1:
                continue
            canonical = next(iter(values))
            candidates.append((self.value_counts[field][canonical], len(alias), canonical))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return candidates[0][2]

    def _query_mentions_domain(self, normalized_query: str, domain: str) -> bool:
        for alias in alias_variants(domain):
            if f" {alias.replace('-', ' ')} " in normalized_query:
                return True
        return False

    def match_query(self, query: str) -> dict[str, str]:
        normalized_query = query_text(query)
        matched: dict[str, str] = {}

        domain = self._match_field("domain", normalized_query)
        hall = self._match_field("hall", normalized_query)
        room_any = self._match_field("room", normalized_query)
        topic_any = self._match_field("topic", normalized_query)
        room = self._match_field("room", normalized_query, domain=domain) or room_any
        topic = self._match_field("topic", normalized_query, domain=domain) or topic_any

        room_domain = (
            next(iter(self.domains_by_room[room_any]))
            if room_any and len(self.domains_by_room[room_any]) == 1
            else None
        )
        topic_domain = (
            next(iter(self.domains_by_topic[topic_any]))
            if topic_any and len(self.domains_by_topic[topic_any]) == 1
            else None
        )
        # If room and topic each infer a domain but disagree, infer nothing
        # rather than letting one silently win and route to the wrong corpus.
        if room_domain and topic_domain:
            inferred_domain = room_domain if room_domain == topic_domain else None
        else:
            inferred_domain = room_domain or topic_domain

        if inferred_domain:
            inferred_count = self.value_counts["domain"].get(inferred_domain, 0)
            direct_count = self.value_counts["domain"].get(domain, 0) if domain else 0
            if not domain or (
                inferred_count >= direct_count and self._query_mentions_domain(normalized_query, inferred_domain)
            ):
                domain = inferred_domain

        if domain:
            room = room or self._match_field("room", normalized_query, domain=domain)
            topic = topic or self._match_field("topic", normalized_query, domain=domain)
            matched["domain"] = domain
        if hall:
            matched["hall"] = hall
        if room:
            matched["room"] = room
        if topic:
            matched["topic"] = topic
        return matched

    def build_routes(self, query: str) -> list[QueryRoute]:
        matched = self.match_query(query)
        routes: list[QueryRoute] = []
        seen: set[str] = set()

        def add_route(filters: dict[str, Any] | None, description: str) -> None:
            key = json.dumps(filters, sort_keys=True) if filters is not None else "null"
            if key in seen:
                return
            seen.add(key)
            routes.append(QueryRoute(filters=filters, description=description, matched=dict(matched)))

        domain = matched.get("domain")
        hall = matched.get("hall")
        room = matched.get("room")
        topic = matched.get("topic")

        if domain and topic:
            add_route({"AND": [{"domain": domain}, {"topic": topic}]}, f"domain={domain}, topic={topic}")
        if domain and room:
            add_route({"AND": [{"domain": domain}, {"room": room}]}, f"domain={domain}, room={room}")
        if domain and hall:
            add_route({"AND": [{"domain": domain}, {"hall": hall}]}, f"domain={domain}, hall={hall}")
        if domain:
            add_route({"domain": domain}, f"domain={domain}")
        if room and not domain:
            add_route({"room": room}, f"room={room}")
        if topic and not domain:
            add_route({"topic": topic}, f"topic={topic}")
        if hall and not domain:
            add_route({"hall": hall}, f"hall={hall}")
        add_route(None, "global")
        return routes

    def document_url(self, record_id: str, metadata: dict[str, Any]) -> str:
        source_url = metadata.get("source_url")
        if isinstance(source_url, str) and source_url.startswith(("http://", "https://")):
            return source_url
        source_ref = metadata.get("source_ref")
        if isinstance(source_ref, str) and source_ref.startswith(("http://", "https://")):
            return source_ref
        return f"reliquary://record/{record_id}"

    def fetch_document(self, record_id: str) -> dict[str, Any] | None:
        record = self.records_by_id.get(record_id)
        if not record:
            return None
        return {
            "id": record.import_record_id,
            "title": record.title,
            "text": record.text,
            "url": self.document_url(record.import_record_id, record.metadata),
            "metadata": record.metadata,
        }
