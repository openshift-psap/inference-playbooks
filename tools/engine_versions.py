"""Resolve auditable engine metadata; never infer an engine from an image tag."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator

from recipe_evidence import git_file, load_unique_yaml


def load_engine_index(repo: Path) -> dict:
    """Load the single maintained default-runtime release index."""
    index = load_unique_yaml((repo / "engine-versions/index.yaml").read_text())
    schema = json.loads((repo / "schema/engine-index.schema.json").read_text())
    Draft202012Validator(schema).validate(index)
    versions = set()
    for entry in index["releases"]:
        if entry["version"] in versions:
            raise ValueError(f"duplicate release component mapping: {entry['version']}")
        versions.add(entry["version"])
    return index


def release_vllm_version(index: dict, release: str) -> str | None:
    """Exact release lookup; no implicit patch version or carry-forward."""
    return next((entry["vllm_version"] for entry in index["releases"] if entry["version"] == release), None)


def effective_image_metadata(serving: dict, overrides: dict) -> tuple[str, dict, dict | None]:
    image = overrides.get("image", serving["image"])
    inherit = image == serving["image"]
    usage = overrides.get("image_usage", serving.get("image_usage", {}) if inherit else {})
    declaration = overrides.get("engine", serving.get("engine") if inherit else None)
    return image, usage, declaration


def resolve_engine(serving: dict, overrides: dict, platform: dict, index: dict) -> dict:
    """Return state, image, version, source and reason after image replacement.

    A declaration binds to an exact image string. Image replacement discards
    the base declaration; an override declaration must bind to the new image.
    """
    image, usage, declaration = effective_image_metadata(serving, overrides)
    mapped = None
    if usage.get("kind") == "default" and platform.get("stack") == "rhoai" and usage.get("variant") in index["variants"]:
        mapped = release_vllm_version(index, platform.get("version", ""))
    if declaration and declaration["image"] != image:
        return dict(state="error", image=image, version=None, source=None, reason="engine declaration image differs from effective image")
    if declaration and mapped and normalize_version(declaration["version"]) != mapped:
        return dict(state="error", image=image, version=None, source=None, reason="default runtime declaration conflicts with release index; identify a custom image if using a different engine")
    if not usage:
        return dict(state="unknown", image=image, version=None, source=None, reason="image_usage must identify default or custom image")
    if declaration:
        return dict(state="resolved", image=image, version=normalize_version(declaration["version"]),
                    source="declaration", evidence=declaration["source"], reason=None)
    if mapped:
        return dict(state="resolved", image=image, version=mapped, source="release-index",
                    evidence=dict(index["source"]), reason=None)
    reason = "custom image requires image-bound serving.engine or platform engine metadata" if usage.get("kind") == "custom" else "no release mapping for the declared default runtime and variant"
    return dict(state="unknown", image=image, version=None, source=None, reason=reason)


def engine_errors(serving: dict, overrides: dict, platform: dict, index: dict, require_metadata: bool = False) -> list[str]:
    """Validate declaration bindings even when a platform replaces the image."""
    if serving.get("engine") and serving["engine"]["image"] != serving["image"]:
        return ["base engine declaration image differs from serving.image"]
    _, usage, declaration = effective_image_metadata(serving, overrides)
    if usage.get("kind") == "custom" and not str(usage.get("note", "")).strip():
        return ["custom image requires a non-empty image_usage.note"]
    if usage.get("kind") == "default" and not usage.get("variant"):
        return ["default runtime requires image_usage.variant"]
    resolution = resolve_engine(serving, overrides, platform, index)
    if resolution["state"] == "error":
        return [resolution["reason"]]
    if resolution["state"] == "unknown" and (require_metadata or usage):
        return [resolution["reason"]]
    return []


def baseline_images(repo: Path, recipe_path: Path, revision: str) -> dict[tuple[str, str], str]:
    """Image identity of each platform at the baseline; absent recipe => new."""
    relative = recipe_path.relative_to(repo)
    try:
        previous = load_unique_yaml(git_file(repo, revision, relative.as_posix()))
    except subprocess.CalledProcessError:
        return {}
    images = {}
    for platform in previous.get("platforms", []):
        if platform.get("blocked"):
            continue
        override_ref = relative.parent / platform["overrides"]
        overrides = load_unique_yaml(git_file(repo, revision, override_ref.as_posix()))
        images[(platform["stack"], platform["version"])] = overrides.get("image", previous["serving"]["image"])
    return images


RELEASE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def normalize_version(version: str) -> str:
    """Normalize only upstream release spellings; preserve opaque builds."""
    return version[1:] if RELEASE.fullmatch(version) and version.startswith("v") else version


def compare_versions(left: str | None, right: str | None) -> int | None:
    """-1/0/1 for comparable releases; opaque builds support exact equality only.

    Prerelease/development/vendor strings are deliberately not ordered, even
    if a packaging library could impose an order not justified by provenance.
    """
    if left is None or right is None:
        return None
    left, right = normalize_version(left), normalize_version(right)
    if left == right:
        return 0
    if not RELEASE.fullmatch(left) or not RELEASE.fullmatch(right):
        return None
    a, b = tuple(map(int, left.split("."))), tuple(map(int, right.split(".")))
    return (a > b) - (a < b)
