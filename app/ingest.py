#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


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
        dims = int(env_dims) if env_dims else vector_config.get("embedding_model_dims", 1536)
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
        dims = int(env_dims) if env_dims else vector_config.get("embedding_model_dims", 768)
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
    parser.add_argument("--limit", type=int, default=None, help="Only ingest the first N unique records.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported without calling Mem0.")
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
    for index, item in enumerate(ordered, start=1):
        metadata = dict(item["metadata"])
        metadata["import_record_id"] = item["id"]
        memory.add(item["text"], user_id=args.user_id, metadata=metadata, infer=args.infer)
        title = metadata.get("title", "<untitled>")
        print(f"[{index}/{len(ordered)}] Imported: {title}")


if __name__ == "__main__":
    main()
