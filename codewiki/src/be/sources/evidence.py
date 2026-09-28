"""Lossless Pega evidence records and a local cache of selected documents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PegaEntity:
    id: str
    name: str
    entity_type: str
    rule_type: str
    rule_category: str
    class_name: str
    ruleset: str
    is_external: bool
    is_embedded: bool
    document_ids: tuple[str, ...]
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, raw: dict[str, Any], document_ids: list[str] | None = None) -> PegaEntity:
        entity_id = str(raw.get("id") or "")
        if not entity_id:
            raise ValueError("Pega entity has no graph ID")
        return cls(
            id=entity_id,
            name=str(raw.get("name") or raw.get("canonical_identity") or entity_id),
            entity_type=str(raw.get("entity_type") or ""),
            rule_type=str(raw.get("rule_type") or ""),
            rule_category=str(raw.get("rule_category") or ""),
            class_name=str(raw.get("class_name") or ""),
            ruleset=str(raw.get("ruleset") or ""),
            is_external=bool(raw.get("is_external", False) or "ExternalReference" in (raw.get("_labels") or [])),
            is_embedded=bool(raw.get("is_embedded", False) or "PegaEmbeddedEntity" in (raw.get("_labels") or [])),
            document_ids=tuple(sorted(set(document_ids or []))),
            properties=dict(raw),
        )

    @property
    def documented_rule(self) -> bool:
        return bool(self.document_ids and self.rule_type) and not self.is_external and not self.is_embedded

    def card(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "entity_type": self.entity_type,
            "rule_type": self.rule_type,
            "rule_category": self.rule_category,
            "class_name": self.class_name,
            "ruleset": self.ruleset,
            "is_external": self.is_external,
            "is_embedded": self.is_embedded,
            "document_ids": list(self.document_ids),
        }


@dataclass(frozen=True)
class PegaRelationship:
    id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    document_id: str
    properties: dict[str, Any]

    @classmethod
    def from_result(cls, raw: dict[str, Any]) -> PegaRelationship:
        properties = dict(raw.get("evidence") or raw.get("properties") or raw)
        relation_id = str(properties.get("id") or raw.get("id") or "")
        source_id = str(raw.get("source_entity_id") or properties.get("source_entity_id") or "")
        target_id = str(raw.get("target_entity_id") or properties.get("target_entity_id") or "")
        relation_type = str(raw.get("relationship_type") or properties.get("relationship_type") or "")
        if not all((relation_id, source_id, target_id, relation_type)):
            raise ValueError(f"Incomplete directed Pega relationship: {raw!r}")
        qualifiers = properties.get("qualifiers_json")
        if isinstance(qualifiers, str) and qualifiers:
            parsed = json.loads(qualifiers)
            if not isinstance(parsed, dict):
                raise ValueError(f"Invalid qualifiers_json on edge {relation_id}")
            for key, value in parsed.items():
                if key in properties and properties[key] != value:
                    raise ValueError(f"Conflicting {key} qualifier on edge {relation_id}")
        return cls(
            id=relation_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            relationship_type=relation_type,
            document_id=str(properties.get("document_id") or ""),
            properties=properties,
        )


@dataclass(frozen=True)
class PegaDocument:
    id: str
    title: str
    markdown: str
    source_path: str
    entity_ids: tuple[str, ...]
    properties: dict[str, Any]

    @property
    def sha256(self) -> str:
        return digest(self.markdown)

    @classmethod
    def from_result(cls, document_id: str, raw: dict[str, Any]) -> PegaDocument:
        metadata = raw.get("document")
        if not isinstance(metadata, dict) or str(metadata.get("id")) != document_id:
            raise ValueError(f"Mismatched document returned for {document_id}")
        markdown = raw.get("markdown")
        if not isinstance(markdown, str):
            raise ValueError(f"Document {document_id} has no Markdown")
        entities = raw.get("entities") or []
        return cls(
            id=document_id,
            title=str(metadata.get("title") or document_id),
            markdown=markdown,
            source_path=str(metadata.get("source_path") or ""),
            entity_ids=tuple(sorted(str(item["id"]) for item in entities if isinstance(item, dict) and item.get("id"))),
            properties=dict(metadata),
        )

    def section_bounds(self, heading: str) -> tuple[int, int]:
        """Return inclusive 1-based line bounds for an exact Markdown section."""
        lines = self.markdown.splitlines()
        for index, line in enumerate(lines):
            if line.lstrip("# ").strip().casefold() != heading.casefold() or not line.startswith("#"):
                continue
            level = len(line) - len(line.lstrip("#"))
            end = len(lines)
            for following in range(index + 1, len(lines)):
                candidate = lines[following]
                if candidate.startswith("#") and len(candidate) - len(candidate.lstrip("#")) <= level:
                    end = following
                    break
            return index + 1, end
        raise KeyError(f"Section {heading!r} not found in {self.id}")

    def section(self, heading: str, *, max_chars: int = 16000) -> tuple[str, int]:
        """Return a bounded Markdown section preview and its first line."""
        start, end = self.section_bounds(heading)
        body = "\n".join(self.markdown.splitlines()[start - 1 : end]).strip()
        if len(body) > max_chars:
            return body[:max_chars] + "\n[Section truncated; request a line range]", start
        return body, start


class EvidenceCache:
    """Cache only graph-selected official Markdown and its MCP metadata."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._documents: dict[str, PegaDocument] = {}

    def put_document(self, document: PegaDocument) -> Path:
        filename = digest(document.id)[:24]
        markdown_path = self.root / f"{filename}.md"
        metadata_path = self.root / f"{filename}.json"
        markdown_path.write_text(document.markdown, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(
                {
                    "document_id": document.id,
                    "title": document.title,
                    "source_path": document.source_path,
                    "entity_ids": document.entity_ids,
                    "sha256": document.sha256,
                    "properties": document.properties,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        self._documents[document.id] = document
        return markdown_path

    def get_document(self, document_id: str) -> PegaDocument | None:
        return self._documents.get(document_id)

    def load_document(self, document_id: str, manifest_entry: dict[str, Any]) -> PegaDocument:
        """Load and verify a document captured by a previous snapshot run."""
        filename = digest(document_id)[:24]
        expected_path = f"{self.root.name}/{filename}.md"
        if manifest_entry.get("cache_path") != expected_path:
            raise ValueError(f"Unexpected cache path for {document_id}")
        markdown_path = self.root / f"{filename}.md"
        metadata_path = self.root / f"{filename}.json"
        for path in (markdown_path, metadata_path):
            try:
                path.resolve(strict=True).relative_to(self.root)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Cached evidence path is missing or escapes cache: {path}") from exc
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("document_id") != document_id:
            raise ValueError(f"Mismatched cached document metadata for {document_id}")
        markdown = markdown_path.read_text(encoding="utf-8")
        actual_hash = digest(markdown)
        if metadata.get("sha256") != actual_hash or manifest_entry.get("sha256") != actual_hash:
            raise ValueError(f"Cached official Markdown hash differs for {document_id}")
        if metadata.get("source_path") != manifest_entry.get("source_path"):
            raise ValueError(f"Cached source path differs for {document_id}")
        if sorted(metadata.get("entity_ids") or []) != sorted(manifest_entry.get("entity_ids") or []):
            raise ValueError(f"Cached linked entities differ for {document_id}")
        document = PegaDocument(
            id=document_id,
            title=str(metadata.get("title") or document_id),
            markdown=markdown,
            source_path=str(metadata.get("source_path") or ""),
            entity_ids=tuple(sorted(str(item) for item in metadata.get("entity_ids") or [])),
            properties=dict(metadata.get("properties") or {}),
        )
        self._documents[document_id] = document
        return document

    def markdown_path(self, document_id: str) -> Path:
        return self.root / f"{digest(document_id)[:24]}.md"

    def manifest(self) -> dict[str, Any]:
        return {
            document_id: {
                "sha256": document.sha256,
                "cache_path": str(self.markdown_path(document_id).relative_to(self.root.parent)),
                "source_path": document.source_path,
                "entity_ids": list(document.entity_ids),
            }
            for document_id, document in sorted(self._documents.items())
        }
