#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


IMPORT_BOOKKEEPING_KEYS = {"import_record_id", "import_content_hash", "source_group"}


def _clean_import_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metadata.items() if key not in IMPORT_BOOKKEEPING_KEYS}


def record_content_hash(item: dict[str, Any]) -> str:
    payload = {
        "text": item.get("text") or "",
        "metadata": _clean_import_metadata(dict(item.get("metadata") or {})),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def import_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(item.get("metadata") or {})
    metadata["import_record_id"] = item["id"]
    metadata["import_content_hash"] = record_content_hash(item)
    metadata["source_group"] = "imported"
    return metadata


def _memory_results(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        hits = result.get("results")
        return hits if isinstance(hits, list) else []
    return result if isinstance(result, list) else []


def existing_import_index(memory: Any, *, user_id: str) -> dict[str, dict[str, Any]]:
    if not hasattr(memory, "get_all"):
        return {}
    result = memory.get_all(user_id=user_id)
    index: dict[str, dict[str, Any]] = {}
    for hit in _memory_results(result):
        metadata = hit.get("metadata") or {}
        import_id = metadata.get("import_record_id")
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
        index[str(import_id)] = {"id": str(memory_id), "content_hash": str(content_hash)}
    return index


def ingest_records(
    memory: Any,
    records: list[dict[str, Any]],
    *,
    user_id: str,
    infer: bool = False,
    incremental: bool = False,
) -> dict[str, int]:
    existing = existing_import_index(memory, user_id=user_id) if incremental else {}
    summary = {"selected": len(records), "added": 0, "updated": 0, "skipped": 0}

    for item in records:
        metadata = import_metadata(item)
        current_hash = metadata["import_content_hash"]
        previous = existing.get(str(item["id"]))
        if previous and previous["content_hash"] == current_hash:
            summary["skipped"] += 1
            continue
        if previous and hasattr(memory, "update"):
            memory.update(previous["id"], item["text"], metadata)
            summary["updated"] += 1
            continue
        memory.add(item["text"], user_id=user_id, metadata=metadata, infer=infer)
        summary["added"] += 1

    return summary


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Invalid JSON in {path} at line {lineno}: {exc}") from exc
            if not isinstance(item, dict):
                raise SystemExit(f"Invalid record in {path} at line {lineno}: expected a JSON object")
            if "id" not in item or "text" not in item or "metadata" not in item:
                raise SystemExit(
                    f"Invalid record in {path} at line {lineno}: required keys are id, text, metadata"
                )
            if not isinstance(item["metadata"], dict):
                raise SystemExit(f"Invalid record in {path} at line {lineno}: metadata must be an object")
            records.append(item)
    return records


def parse_embedder_dims(env_dims: str | None, fallback: int) -> int:
    if not env_dims:
        return fallback
    try:
        return int(env_dims)
    except ValueError as exc:
        raise SystemExit("MEM0_EMBEDDER_DIMS must be an integer.") from exc


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

    env_provider = os.getenv("MEM0_EMBEDDER_PROVIDER")
    env_model = os.getenv("MEM0_EMBEDDER_MODEL")
    env_base_url = os.getenv("MEM0_EMBEDDER_BASE_URL")
    env_api_key = os.getenv("MEM0_EMBEDDER_API_KEY")
    env_dims = os.getenv("MEM0_EMBEDDER_DIMS")

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
        "Add one to your config file, or set MEM0_EMBEDDER_PROVIDER/MODEL/BASE_URL/API_KEY env vars before running."
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Ingest a JSONL corpus into Mem0. Each line is a record: "
            '{"id": "...", "text": "...", "metadata": {...}}. Records are '
            "de-duplicated by `id`."
        )
    )
    parser.add_argument(
        "dataset",
        nargs="+",
        help="One or more JSONL files (or directories of *.jsonl) to ingest.",
    )
    parser.add_argument("--config", default="~/.mem0/config.yaml", help="Path to your Mem0 config file.")
    parser.add_argument("--user-id", default="default", help="Mem0 user_id to attach to every imported record.")
    parser.add_argument("--limit", type=non_negative_int, default=None, help="Only ingest the first N unique records.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without calling Mem0.")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Skip unchanged records and update changed records by stable JSONL id.",
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
    args = parser.parse_args()

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
    summary = ingest_records(
        memory,
        ordered,
        user_id=args.user_id,
        infer=args.infer,
        incremental=args.incremental,
    )
    print(
        "Ingest complete: "
        f"{summary['added']} added, {summary['updated']} updated, "
        f"{summary['skipped']} skipped, {summary['selected']} selected."
    )


if __name__ == "__main__":
    main()
