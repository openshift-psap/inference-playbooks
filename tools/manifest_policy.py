"""Shared policy for author-supplied Kubernetes manifests, not runtime objects."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

import yaml

from recipe_evidence import load_unique_yaml_all


SERVICE_POLICY = (
    "Author-supplied Service objects are prohibited in recipe submissions; "
    "use operator-managed networking. Remove this object from the submitted bundle."
)


def load_manifest_documents(path: Path, text: str) -> list[object]:
    """Use the existing duplicate-key-safe YAML loader, or strict JSON syntax."""
    return [json.loads(text)] if path.suffix.lower() == ".json" else load_unique_yaml_all(text)


def service_errors(path: Path, documents: list[object]) -> list[str]:
    """Check document roots and Kubernetes List items, never arbitrary data/strings."""
    errors = []

    def check(value: object, location: str, ancestors: frozenset[int]) -> None:
        if not isinstance(value, dict) or id(value) in ancestors:
            return
        if value.get("kind") in ("Service", "ServiceList"):
            metadata = value.get("metadata")
            name = metadata.get("name", metadata.get("generateName", "<unnamed>")) if isinstance(metadata, dict) else "<unnamed>"
            errors.append(f"{path}: {location}: {value['kind']}/{name}: {SERVICE_POLICY}")
        if value.get("kind") == "List" and isinstance(value.get("items"), list):
            for index, item in enumerate(value["items"]):
                check(item, f"{location}.items[{index}]", ancestors | {id(value)})

    for index, document in enumerate(documents, 1):
        check(document, f"document {index}", frozenset())
    return errors


def validate_manifest_text(path: Path, text: str) -> list[str]:
    """Check parsed manifest bytes, including not-yet-written renderer outputs."""
    try:
        return service_errors(path, load_manifest_documents(path, text))
    except (ValueError, yaml.YAMLError) as error:
        return [f"{path}: cannot parse manifest: {error}"]


def validate_recipe_manifest_files(
    recipe_dir: Path, recipe: dict, *, regenerated_outputs: set[Path] | None = None,
) -> list[str]:
    """Check config/output trees and explicit pins/manifests inside one recipe.

    Raw intake is parsed separately by validate_raw_manifest_intake. Exclude
    prose, benchmarks, and results unless explicitly referenced as a manifest.
    Only rendering may omit destinations it will replace; config, pins, and
    explicitly hand-authored manifests remain inputs even at those paths.
    """
    paths = set()
    protected = set()
    for folder in ("config", "manifests"):
        files = {path for path in (recipe_dir / folder).rglob("*")
                 if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}}
        paths.update(files)
        if folder == "config":
            protected.update(files)
    platforms = recipe.get("platforms", [])
    deployment = recipe.get("deployment", {})
    manifests = deployment.get("manifests", []) if isinstance(deployment, dict) else []
    references = [entry.get("pinned_manifest") for entry in platforms if isinstance(entry, dict)] if isinstance(platforms, list) else []
    protected.update(recipe_dir / reference for reference in references if isinstance(reference, str))
    if isinstance(manifests, list):
        references.extend(entry.get("path") for entry in manifests if isinstance(entry, dict))
        protected.update(recipe_dir / entry["path"] for entry in manifests
                         if isinstance(entry, dict) and isinstance(entry.get("path"), str)
                         and entry.get("generated") is False)
    for reference in references:
        if isinstance(reference, str):
            path = recipe_dir / reference
            if path.is_file() and path.resolve().is_relative_to(recipe_dir.resolve()):
                paths.add(path)
    errors = []
    seen = set()
    omitted = {path.resolve() for path in regenerated_outputs or ()} - {path.resolve() for path in protected}
    for path in sorted(paths):
        resolved = path.resolve()
        if not resolved.is_relative_to(recipe_dir.resolve()):
            errors.append(f"{path}: manifest file escapes recipe directory")
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved in omitted:
            continue
        # Raw files are already checked with intake syntax/layout diagnostics.
        if resolved.is_relative_to((recipe_dir / "raw-manifest").resolve()):
            continue
        try:
            errors.extend(validate_manifest_text(path, path.read_text()))
        except (OSError, UnicodeError) as error:
            errors.append(f"{path}: cannot read manifest: {error}")
    return errors


def report_service_policy_errors(repo: Path, errors: list[str]) -> None:
    """Augment normal local diagnostics with CI file annotations and a summary."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    violations = [error for error in errors if SERVICE_POLICY in error]
    if not violations:
        return

    def escape(value: str) -> str:
        return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    summary = []
    for error in violations:
        filename, _, message = error.partition(": ")
        path = Path(filename)
        if path.is_absolute() and path.is_relative_to(repo):
            filename = path.relative_to(repo).as_posix()
        property_value = escape(filename).replace(":", "%3A").replace(",", "%2C")
        print(f"::error file={property_value},title=Prohibited Service::{escape(message)}")
        summary.append(f"<li><code>{html.escape(filename)}</code>: {html.escape(message)}</li>")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
            output.write("\n## Prohibited author-supplied Services\n\n<ul>\n" + "\n".join(summary) + "\n</ul>\n")
