#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any

# Mem0 reports anonymous usage to PostHog unless MEM0_TELEMETRY is false, and
# reads the variable once, when mem0 is first imported. Default it off before
# anything imports mem0; an explicit MEM0_TELEMETRY=true still opts in.
os.environ.setdefault("MEM0_TELEMETRY", "false")

from catalog import slugify_value  # noqa: E402
from metadata_policy import honours_import_id, taxonomy_match_keys  # noqa: E402


IMPORT_BOOKKEEPING_KEYS = {"import_record_id", "import_content_hash", "source_group"}
# Taxonomy fields are stored as the same slugs the catalog advertises and routes
# on, so a route filter such as {"domain": "work-notes"} matches the payload.
TAXONOMY_FIELDS = ("domain", "hall", "room", "topic")
# Mem0 owns the top-level created_at/updated_at of every memory: add() sets
# updated_at = created_at and update() sets updated_at = now. The corpus's own
# dates are kept under these namespaced keys so the recency tiebreaker uses them.
SOURCE_DATE_KEYS = {
    "source_updated_at": ("updated_at", "updatedAt"),
    "source_created_at": ("created_at", "createdAt", "timestamp", "date"),
}


def _clean_import_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key not in IMPORT_BOOKKEEPING_KEYS}


def record_content_hash(item: dict[str, Any]) -> str:
    payload = {
        "text": item.get("text") or "",
        "metadata": _clean_import_metadata(dict(item.get("metadata") or {})),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_date(metadata: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip()):
            return value
    return None


def derived_import_fields(metadata: dict[str, Any]) -> dict[str, Any]:
    """Fields the importer derives from a record's own metadata.

    They are not part of `import_content_hash` (which hashes the record as
    written in the JSONL), so adding a derived field never changes the hash.
    """
    derived: dict[str, Any] = {}
    for field in TAXONOMY_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str) and value:
            derived[field] = slugify_value(value)
    for target, keys in SOURCE_DATE_KEYS.items():
        if target in metadata:
            continue  # an explicit value in the corpus wins
        value = _source_date(metadata, keys)
        if value is not None:
            derived[target] = value
    return derived


def import_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(item.get("metadata") or {})
    metadata.update(derived_import_fields(metadata))
    metadata["import_record_id"] = item["id"]
    metadata["import_content_hash"] = record_content_hash(item)
    metadata["source_group"] = "imported"
    return metadata


def _derived_fields_current(stored: dict[str, Any], item: dict[str, Any]) -> bool:
    derived = derived_import_fields(dict(item.get("metadata") or {}))
    return all(stored.get(key) == value for key, value in derived.items())


def _memory_results(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        hits = result.get("results")
        return hits if isinstance(hits, list) else []
    return result if isinstance(result, list) else []


def existing_import_index(memory: Any, *, user_id: str) -> dict[str, dict[str, Any]]:
    if not hasattr(memory, "get_all"):
        return {}
    params = inspect.signature(memory.get_all).parameters
    kwargs = {"user_id": user_id} if "user_id" in params else {"filters": {"user_id": user_id}}
    # Mem0 2.x defaults to just 20 records and rejects top-level user_id.
    # Its public API has no cursor; grow the prefix until it contains the whole
    # corpus, so incremental imports cannot duplicate records beyond page one.
    limit_key = "top_k" if "top_k" in params else "limit" if "limit" in params else None
    limit = 1000
    while True:
        result = memory.get_all(**kwargs, **({limit_key: limit} if limit_key else {}))
        if limit_key is None or len(_memory_results(result)) < limit:
            break
        limit *= 2
    index: dict[str, dict[str, Any]] = {}
    for hit in _memory_results(result):
        metadata = hit.get("metadata") or {}
        # A user-write or compiled page may carry a forged import_record_id;
        # mapping it would make the import overwrite or skip the wrong vector.
        import_id = metadata.get("import_record_id") if honours_import_id(metadata) else None
        memory_id = hit.get("id")
        if not import_id or not memory_id:
            continue
        content_hash = metadata.get("import_content_hash")
        if not content_hash:
            content_hash = record_content_hash({
                "id": import_id,
                "text": hit.get("memory") or "",
                "metadata": metadata,
            })
        copy = {"id": str(memory_id), "content_hash": str(content_hash), "metadata": dict(metadata)}
        entry = index.get(str(import_id))
        if entry is None:
            # The first copy's fields sit at the top level; "copies" holds every
            # trusted copy, including duplicates that --no-incremental re-runs
            # (once the default) left behind.
            index[str(import_id)] = {**copy, "copies": [copy]}
        else:
            entry["copies"].append(copy)
    return index


def _taxonomy_pairs(domain: Any, topic: Any) -> set[tuple[str, str]]:
    """(domain, topic) match-key pairs, so a page filed under an older spelling
    of either value ("Work Notes", "g-nl-k") meets the slugs imports store."""
    return {(d, t) for d in taxonomy_match_keys(domain) for t in taxonomy_match_keys(topic)}


def _fan_out_bulk(page_registry, touched_ids: set[str], touched_tax: set) -> None:
    """One-pass staleness fan-out after a bulk import (avoids O(records*pages))."""
    try:
        for page in page_registry.list(status="current"):
            # A page that declares derived_from is judged on its sources alone;
            # the domain+topic fallback applies only to provenance-less pages (#69).
            if (touched_ids & set(page.derived_from or [])) or (
                not page.derived_from
                and not touched_tax.isdisjoint(_taxonomy_pairs(page.domain, page.topic))
            ):
                page_registry.set_status(page.slug, "stale")
    except Exception as exc:
        print(f"[ingest] staleness fan-out skipped: {exc}", file=sys.stderr)


def ingest_records(
    memory: Any,
    records: list[dict[str, Any]],
    *,
    user_id: str,
    infer: bool = False,
    incremental: bool = True,
    page_registry=None,
) -> dict[str, int]:
    for position, item in enumerate(records, start=1):
        problem = record_problem(item)
        if problem:
            # Validate everything before the first write, so a bad record can
            # never leave a half-imported corpus behind.
            raise ValueError(f"Invalid record #{position}: {problem}")
    existing = existing_import_index(memory, user_id=user_id) if incremental else {}
    summary = {"selected": len(records), "added": 0, "updated": 0, "skipped": 0, "duplicates": 0}
    touched_ids: set[str] = set()
    touched_tax: set[tuple[str, str]] = set()

    # Invalidation is recorded before each write and fanned out in `finally`:
    # a run that stops halfway has already updated some records, which the
    # retry skips as current, so their dependent pages must be marked now.
    try:
        for item in records:
            metadata = import_metadata(item)
            current_hash = metadata["import_content_hash"]
            previous = existing.get(str(item["id"]))
            copies = previous["copies"] if previous else []
            summary["duplicates"] += max(0, len(copies) - 1)
            if previous and not hasattr(memory, "update"):
                if previous["content_hash"] == current_hash:
                    summary["skipped"] += 1
                    continue
            elif copies:
                # Every trusted copy is brought to the current text and metadata, so
                # an old duplicate cannot outrank the current one with an obsolete
                # snippet. Nothing is deleted, and a run that stops halfway leaves
                # the remaining copies outdated for the next run to finish.
                changed = [copy for copy in copies if copy["content_hash"] != current_hash]
                # Same content, but imported before the importer derived slugged
                # taxonomy and source dates: rewrite the metadata once, without
                # marking compiled pages stale.
                migrated = [copy for copy in copies if copy["content_hash"] == current_hash
                            and not _derived_fields_current(copy["metadata"], item)]
                if not changed and not migrated:
                    summary["skipped"] += 1
                    continue
                if changed:
                    touched_ids.add(str(item["id"]))
                    touched_tax |= _taxonomy_pairs(metadata.get("domain"), metadata.get("topic"))
                    for copy in changed:  # pages filed under the taxonomy the record had
                        touched_tax |= _taxonomy_pairs(copy["metadata"].get("domain"),
                                                       copy["metadata"].get("topic"))
                for copy in changed + migrated:
                    memory.update(copy["id"], item["text"], metadata)
                summary["updated"] += 1
                continue
            touched_ids.add(str(item["id"]))
            touched_tax |= _taxonomy_pairs(metadata.get("domain"), metadata.get("topic"))
            memory.add(item["text"], user_id=user_id, metadata=metadata, infer=infer)
            summary["added"] += 1
    finally:
        if page_registry is not None and (touched_ids or touched_tax):
            _fan_out_bulk(page_registry, touched_ids, touched_tax)

    return summary


def record_problem(item: Any) -> str | None:
    """Why a corpus record cannot be imported, or None when it is valid."""
    if not isinstance(item, dict):
        return "expected a JSON object"
    if "id" not in item or "text" not in item or "metadata" not in item:
        return "required keys are id, text, metadata"
    if not isinstance(item["id"], str) or not item["id"].strip():
        return "id must be a non-empty string"
    if not isinstance(item["text"], str) or not item["text"].strip():
        return "text must be a non-empty string"
    if item["metadata"] is not None and not isinstance(item["metadata"], dict):
        return "metadata must be an object"
    return None


def read_jsonl(path: Path) -> list[dict]:
    records = []
    # utf-8-sig: a byte-order mark (Windows editors add one) must not break line 1.
    with path.open("r", encoding="utf-8-sig") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON in {path} at line {lineno}: {exc}") from exc
            problem = record_problem(item)
            if problem is None and not isinstance(item["metadata"], dict):
                problem = "metadata must be an object"
            if problem:
                raise SystemExit(f"Invalid record in {path} at line {lineno}: {problem}")
            records.append(item)
    return records


def parse_embedder_dims(env_dims: str | None, fallback: int) -> int:
    if not env_dims:
        return fallback
    try:
        return int(env_dims)
    except ValueError as exc:
        raise SystemExit("RELIQUARY_EMBEDDER_DIMS must be an integer.") from exc


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("--limit must be >= 0")
    return parsed


def expand_config_values(value):
    if isinstance(value, dict):
        return {key: expand_config_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_config_values(item) for item in value]
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    return value


def synthesize_embedder_config(config: dict) -> dict:
    llm = config.get("llm") or {}
    llm_config = llm.get("config") or {}
    if "openai_api_base" in llm_config and "openai_base_url" not in llm_config:
        llm_config["openai_base_url"] = llm_config.pop("openai_api_base")

    if isinstance(config.get("embedder"), dict):
        return config

    llm_provider = llm.get("provider")
    vector_store = config.get("vector_store") or {}
    vector_config = vector_store.get("config") or {}

    env_provider = os.getenv("RELIQUARY_EMBEDDER_PROVIDER")
    env_model = os.getenv("RELIQUARY_EMBEDDER_MODEL")
    env_base_url = os.getenv("RELIQUARY_EMBEDDER_BASE_URL")
    env_api_key = os.getenv("RELIQUARY_EMBEDDER_API_KEY")
    env_dims = os.getenv("RELIQUARY_EMBEDDER_DIMS")

    if env_provider:
        dims = parse_embedder_dims(env_dims, vector_config.get("embedding_model_dims", 1536))
        embedder_config = {
            "provider": env_provider,
            "config": {
                "model": env_model,
                "api_key": env_api_key,
                "embedding_dims": dims,
                "openai_base_url": env_base_url,
                "lmstudio_base_url": env_base_url,
            },
        }
        embedder_config["config"] = {k: v for k, v in embedder_config["config"].items() if v is not None}
        config["embedder"] = embedder_config
        vector_config.setdefault("embedding_model_dims", dims)
        return config

    llm_api_base = llm_config.get("openai_base_url")
    llm_api_key = llm_config.get("api_key")
    is_lmstudio_like = llm_provider == "openai" and (
        llm_api_key == "lm-studio" or (isinstance(llm_api_base, str) and ":1234" in llm_api_base)
    )

    if is_lmstudio_like:
        dims = parse_embedder_dims(env_dims, vector_config.get("embedding_model_dims", 768))
        config["embedder"] = {
            "provider": "lmstudio",
            "config": {
                "model": env_model or "nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.f16.gguf",
                "lmstudio_base_url": env_base_url or llm_api_base or "http://localhost:1234/v1",
                "api_key": env_api_key or llm_api_key or "lm-studio",
                "embedding_dims": dims,
            },
        }
        vector_config.setdefault("embedding_model_dims", dims)
        return config

    raise SystemExit(
        "Mem0 config is missing an `embedder` section. "
        "Add one to your config file, or set RELIQUARY_EMBEDDER_PROVIDER/MODEL/BASE_URL/API_KEY env vars before running."
    )


def load_config(config_arg: str) -> dict:
    path = Path(config_arg).expanduser()
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".json":
        config = json.loads(path.read_text(encoding="utf-8"))
    else:
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit(
                "PyYAML is required to read YAML Mem0 configs. Install it with `pip install pyyaml`."
            ) from exc
        config = yaml.safe_load(path.read_text(encoding="utf-8"))

    if not isinstance(config, dict):
        raise SystemExit(f"Config file must contain a mapping/object at the top level: {path}")

    config = expand_config_values(config)
    return synthesize_embedder_config(config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a JSONL corpus into Mem0. Each line is a record: "
            '{"id": "...", "text": "...", "metadata": {...}}. Records are '
            "de-duplicated by `id`: re-running updates changed records and skips "
            "unchanged ones."
        )
    )
    parser.add_argument(
        "dataset",
        nargs="+",
        help="One or more JSONL files (or directories of *.jsonl) to ingest.",
    )
    parser.add_argument("--config", default="~/.mem0/config.yaml", help="Path to your Mem0 config file.")
    parser.add_argument(
        "--user-id",
        default=os.getenv("RELIQUARY_USER_ID") or "default",
        help="Mem0 user_id to attach to every imported record (env RELIQUARY_USER_ID; default 'default', "
        "which is also the server's default). It must match the server's --user-id or the records are invisible.",
    )
    parser.add_argument("--limit", type=non_negative_int, default=None, help="Only ingest the first N unique records.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without calling Mem0.")
    parser.add_argument(
        "--incremental",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Skip unchanged records and update changed records by stable JSONL id (default). "
            "--no-incremental adds every record without looking for earlier imports, so "
            "re-running it on an existing collection duplicates the corpus."
        ),
    )
    parser.add_argument(
        "--infer",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Let Mem0 extract atomic facts with the configured LLM before writing. "
            "Disabled by default so raw records are stored reliably."
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    paths: list[Path] = []
    for entry in args.dataset:
        path = Path(entry).expanduser()
        if path.is_dir():
            paths.extend(sorted(path.glob("*.jsonl")))
        else:
            paths.append(path)

    selected: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            raise SystemExit(f"Dataset not found: {path}")
        for item in read_jsonl(path):
            selected[item["id"]] = item

    ordered = list(selected.values())
    if args.limit is not None:
        ordered = ordered[: args.limit]

    if args.dry_run:
        print(f"Dry run: {len(ordered)} record(s) selected.")
        for item in ordered[:10]:
            title = item["metadata"].get("title", "<untitled>")
            source_ref = item["metadata"].get("source_ref", "")
            print(f"- {title} [{source_ref}]")
        return

    try:
        from mem0 import Memory
    except ImportError as exc:
        raise SystemExit(
            "mem0 is not installed in this environment. Run `pip install mem0ai qdrant-client` first."
        ) from exc

    config = load_config(args.config)
    memory = Memory.from_config(config)
    page_registry = None
    # Mirror the server's default-on behavior: an empty RELIQUARY_COMPILED_COLLECTION
    # disables the layer; unset uses the same default the server uses.
    if os.getenv("RELIQUARY_COMPILED_COLLECTION", "reliquary_compiled"):
        try:
            from blobs import BlobStore
            from compiled import PageRegistry
            page_registry = PageRegistry(
                registry_dir=os.getenv("RELIQUARY_COMPILED_DIR", "/data/compiled"),
                blobs=BlobStore(blob_dir=os.getenv("RELIQUARY_BLOB_DIR", "/data/blobs"), signing_key=b"ingest", max_bytes=0),
            )
        except Exception as exc:
            print(f"[ingest] compiled page registry unavailable: {exc}", file=sys.stderr)
            page_registry = None
    summary = ingest_records(
        memory,
        ordered,
        user_id=args.user_id,
        infer=args.infer,
        incremental=args.incremental,
        page_registry=page_registry,
    )
    print(
        "Ingest complete: "
        f"{summary['added']} added, {summary['updated']} updated, "
        f"{summary['skipped']} skipped, {summary['selected']} selected."
        + (f" Kept {summary['duplicates']} duplicate cop{'y' if summary['duplicates'] == 1 else 'ies'}"
           " of earlier imports in step with their record." if summary["duplicates"] else "")
    )


if __name__ == "__main__":
    main()
