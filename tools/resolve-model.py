#!/usr/bin/env python3
"""Fetch model metadata from HuggingFace and print or apply it to a model.yaml.

Usage:
    python3 tools/resolve-model.py google/gemma-4-26B-A4B-it
    python3 tools/resolve-model.py google/gemma-4-26B-A4B-it --update models/gemma-4/model.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml


def fetch_config(model_id: str) -> dict:
    """Fetch config.json from HuggingFace for a model."""
    url = f"https://huggingface.co/{model_id}/resolve/main/config.json"
    headers = {"User-Agent": "inference-playbooks/resolve-model"}
    hf_token = os.environ.get("HF_TOKEN")
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 401 or error.code == 403:
            raise SystemExit(
                f"Access denied for {model_id} (HTTP {error.code}).\n"
                f"This model may be gated. Use source: manual in your model.yaml\n"
                f"and fill in model_type and architectures from the model card."
            )
        if error.code == 404:
            raise SystemExit(
                f"Model {model_id} not found on HuggingFace (HTTP 404).\n"
                f"Check the model ID and try again, or use source: manual."
            )
        raise SystemExit(f"HTTP error fetching {url}: {error}")
    except urllib.error.URLError as error:
        raise SystemExit(
            f"Network error fetching {url}: {error.reason}\n"
            f"Check your network connection, or use source: manual\n"
            f"and fill in model_type and architectures manually."
        )


def extract_metadata(config: dict) -> dict:
    """Extract model_type and architectures from HuggingFace config.json."""
    metadata = {}
    model_type = config.get("model_type")
    if model_type:
        metadata["model_type"] = model_type
    else:
        print("Warning: config.json has no model_type field.", file=sys.stderr)

    architectures = config.get("architectures")
    if architectures and isinstance(architectures, list):
        metadata["architectures"] = architectures
    else:
        print("Warning: config.json has no architectures field.", file=sys.stderr)

    return metadata


def print_yaml_snippet(metadata: dict) -> None:
    """Print a YAML snippet the contributor can paste into model.yaml."""
    print("# Add to model.yaml:")
    print("source: huggingface")
    if "model_type" in metadata:
        print(f"model_type: {metadata['model_type']}")
    if "architectures" in metadata:
        print("architectures:")
        for arch in metadata["architectures"]:
            print(f"  - {arch}")


def update_model_yaml(path: Path, metadata: dict) -> None:
    """Update an existing model.yaml with fetched metadata."""
    if not path.is_file():
        raise SystemExit(f"File not found: {path}")

    text = path.read_text()
    model = yaml.safe_load(text)
    if not isinstance(model, dict):
        raise SystemExit(f"Expected a YAML mapping in {path}")

    model["source"] = "huggingface"
    if "model_type" in metadata:
        model["model_type"] = metadata["model_type"]
    if "architectures" in metadata:
        model["architectures"] = metadata["architectures"]

    # Write back preserving key order as much as possible by using a custom
    # representer that emits mappings in insertion order.
    class OrderedDumper(yaml.SafeDumper):
        pass

    def represent_dict(dumper: yaml.SafeDumper, data: dict) -> yaml.nodes.Node:
        return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())

    OrderedDumper.add_representer(dict, represent_dict)

    path.write_text(yaml.dump(model, Dumper=OrderedDumper, default_flow_style=False, sort_keys=False))
    print(f"Updated {path}")


def main() -> int:
    """Resolve model metadata from HuggingFace."""
    parser = argparse.ArgumentParser(
        description="Fetch model metadata from HuggingFace for use in model.yaml.",
    )
    parser.add_argument(
        "model_id",
        help="HuggingFace model ID (e.g., google/gemma-4-26B-A4B-it)",
    )
    parser.add_argument(
        "--update",
        type=Path,
        metavar="PATH",
        help="Path to a model.yaml file to update in place",
    )
    arguments = parser.parse_args()

    config = fetch_config(arguments.model_id)
    metadata = extract_metadata(config)

    if not metadata:
        print("No model_type or architectures found in config.json.", file=sys.stderr)
        return 1

    if arguments.update:
        update_model_yaml(arguments.update, metadata)
    else:
        print_yaml_snippet(metadata)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
