"""Pure, bounded Agent Skills export of currently resolved company guidance."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any

CLIENT_SKILLS = {"portable": ".agents/skills", "codex": ".agents/skills",
                 "claude": ".claude/skills", "cursor": ".cursor/skills"}
MAX_EXPORT_BYTES = 256 * 1024
MAX_BUNDLES = 20
MAX_SKILLS = 100
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CAPABILITY = re.compile(r"[a-z][a-z0-9_-]{0,119}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_SECRET = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"
    r"|\b(?:password|api[_ -]?key|access[_ -]?token|authorization)\s*[:=]\s*\S+", re.IGNORECASE,
)


class GuidanceExportError(ValueError):
    """The complete export cannot safely represent this resolution."""


def _text(value: Any, label: str, *, maximum: int = 20000, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise GuidanceExportError(f"Invalid or overbudget guidance {label}")
    if any(ord(char) < 32 and (not multiline or char not in "\n\r\t") for char in value):
        raise GuidanceExportError(f"Control characters are not allowed in guidance {label}")
    if _SECRET.search(value):
        raise GuidanceExportError("Guidance containing credential material cannot be exported")
    return value


def _slug(value: Any, label: str, *, maximum: int = 64) -> str:
    value = _text(value, label, maximum=maximum)
    if not _SLUG.fullmatch(value):
        raise GuidanceExportError(f"Invalid guidance {label}; use a lowercase hyphenated name")
    return value


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise GuidanceExportError("Guidance must contain bounded JSON values") from exc


def _yaml_string(value: str) -> str:
    # YAML folds literal Unicode line breaks even inside a quoted scalar.
    return (json.dumps(value, ensure_ascii=False).replace("\x85", "\\u0085")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _no_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in {
                "password", "api_key", "access_token", "authorization", "credentials", "secret",
            }:
                raise GuidanceExportError("Credential fields cannot be exported as guidance")
            _no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _no_credentials(item)
    elif isinstance(value, str) and _SECRET.search(value):
        raise GuidanceExportError("Guidance containing credential material cannot be exported")


def _date(value: Any, label: str) -> str | None:
    if value is None:
        return None
    _text(value, label, maximum=40)
    try:
        if datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError
    except ValueError as exc:
        raise GuidanceExportError(f"Guidance {label} must be an aware ISO date") from exc
    return value


def _payload(payload: Any) -> dict[str, Any]:
    required = {"title", "description", "business_units", "tasks", "audience", "skills", "rules", "dependencies"}
    if (not isinstance(payload, dict) or not required <= payload.keys()
            or payload.keys() - required - {"connection_ids", "effective_from", "effective_until"}):
        raise GuidanceExportError("Published guidance payload has missing or unsupported fields")
    _text(payload["title"], "title", maximum=240)
    _text(payload["description"], "description", maximum=4000, multiline=True)
    for key in ("business_units", "tasks", "connection_ids"):
        values = payload.get(key, [])
        if not isinstance(values, list) or len(values) > 100:
            raise GuidanceExportError(f"Guidance {key} must be a bounded list")
        for value in values:
            _text(value, key, maximum=240)
    audience = payload["audience"]
    if (not isinstance(audience, dict) or set(audience) != {"company_wide", "subjects", "groups"}
            or type(audience["company_wide"]) is not bool):
        raise GuidanceExportError("Guidance audience must declare company_wide, subjects and groups")
    for key in ("subjects", "groups"):
        if not isinstance(audience[key], list) or len(audience[key]) > 100:
            raise GuidanceExportError("Guidance audience subjects/groups must be bounded lists")
        for item in audience[key]:
            _text(item, "audience " + key, maximum=255)
    for key in ("effective_from", "effective_until"):
        _date(payload.get(key), key)
    if (payload.get("effective_from") and payload.get("effective_until")
            and datetime.fromisoformat(payload["effective_from"]) >= datetime.fromisoformat(payload["effective_until"])):
        raise GuidanceExportError("Guidance validity interval is empty")
    skills, rules, dependencies = (payload[key] for key in ("skills", "rules", "dependencies"))
    if (not isinstance(skills, list) or len(skills) > MAX_SKILLS
            or not isinstance(rules, list) or len(rules) > 100
            or not isinstance(dependencies, list) or len(dependencies) > 100
            or not (skills or rules)):
        raise GuidanceExportError("Guidance skills, rules and dependencies must be bounded and nonempty in meaning")
    names = set()
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != {"name", "description", "instructions", "required_capabilities"}:
            raise GuidanceExportError("Guidance skill must have name, description, instructions and required_capabilities")
        name = _slug(skill["name"], "skill name")
        if name in names:
            raise GuidanceExportError("Guidance skill names collide within a bundle")
        names.add(name)
        _text(skill["description"], "skill description", maximum=1024, multiline=True)
        _text(skill["instructions"], "skill instructions", multiline=True)
        capabilities = skill["required_capabilities"]
        if not isinstance(capabilities, list) or len(capabilities) > 20:
            raise GuidanceExportError("Required capabilities must be a bounded descriptive list")
        for capability in capabilities:
            if not isinstance(capability, str) or not _CAPABILITY.fullmatch(capability):
                raise GuidanceExportError("Required capabilities must be generic identifiers, not tool permissions")
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"key", "statement", "value"}:
            raise GuidanceExportError("Guidance rule must have key, statement and value")
        _text(rule["key"], "rule key", maximum=120)
        _text(rule["statement"], "rule statement", multiline=True)
        value = rule["value"]
        if (value is not None and type(value) not in (bool, int, float, str)
                or type(value) is int and not -(2 ** 63) <= value < 2 ** 63):
            raise GuidanceExportError("Guidance rule values must be bounded JSON scalars")
        if isinstance(value, str) and (len(value) > 4000 or any(ord(char) < 32 and char not in "\n\r\t" for char in value)):
            raise GuidanceExportError("Guidance rule string value is invalid or overbudget")
        _json(rule["value"])
        _no_credentials(rule["value"])
    for dependency in dependencies:
        if (not isinstance(dependency, dict) or set(dependency) != {"connection_id", "snapshot_id", "snapshot_version"}
                or type(dependency["snapshot_version"]) is not int or dependency["snapshot_version"] < 1):
            raise GuidanceExportError("Guidance dependencies need exact connection/snapshot/version bindings")
        _text(dependency["connection_id"], "dependency connection", maximum=240)
        _text(dependency["snapshot_id"], "dependency snapshot", maximum=240)
    return deepcopy(payload)


def _name(slug: str, name: str) -> str:
    full = f"kl-{slug}-{name}"
    if len(full) <= 64:
        return full
    digest = hashlib.sha256(full.encode()).hexdigest()[:12]
    return f"kl-{slug[:22].rstrip('-')}-{name[:22].rstrip('-')}-{digest}"


def _rules(bundles: list[dict[str, Any]]) -> str:
    parts = ["## Company rules\n"]
    for bundle in bundles:
        parts.append(f"### {bundle['slug']} — published revision {bundle['revision']}\n")
        if not bundle["payload"]["rules"]:
            parts.append("This bundle declares no additional rules.\n")
        for rule in bundle["payload"]["rules"]:
            parts.extend([f"Rule key: {_json(rule['key'])}\n", rule["statement"],
                          "\nDeclared value (JSON):\n", "```json\n" + _json(rule["value"]) + "\n```\n"])
    return "\n".join(parts)


_BOOTSTRAP = """# Company guidance bootstrap

Before applying this exported guidance, fetch current company guidance from the
customer's authorized live registry for the same business unit, task and source
scope. Require a ready resolution with no blocked bundles or rule conflicts.
Verify every published revision, digest, validity interval and pinned dependency
listed below remains current and readable. If guidance is revoked, archived,
expired, stale, conflicted or inaccessible, stop and request current guidance.
If the registry cannot be reached or any dependency cannot be verified, stop.

This export is guidance only. It grants no tool permissions, credentials, source
access or automated enforcement. Required capabilities describe prerequisites;
use only interfaces separately authorized by the customer and the harness.
This applicable resolution is not a complete inventory of all company policies.
Do not rewrite existing user or harness rules, or resolve conflicts by omission.
"""


def export_guidance(resolution: dict, *, client: str = "portable") -> dict[str, Any]:
    """Export full published instructions or refuse; no filesystem or network I/O."""
    if not isinstance(client, str) or client not in CLIENT_SKILLS:
        raise GuidanceExportError("Unsupported guidance client; use portable, codex, claude or cursor")
    if (not isinstance(resolution, dict) or resolution.get("status") != "ready"
            or resolution.get("blocked") or resolution.get("conflicts")):
        raise GuidanceExportError("Only ready guidance without blocked bundles or conflicts can be exported")
    if len(_json(resolution).encode()) > MAX_EXPORT_BYTES:
        raise GuidanceExportError("Published guidance input exceeds the 256 KiB budget")
    source = resolution.get("bundles")
    if not isinstance(source, list) or not 1 <= len(source) <= MAX_BUNDLES:
        raise GuidanceExportError("A complete export requires 1-20 published bundles; narrow the applicable scope")
    context = resolution.get("context")
    if (not isinstance(context, dict) or set(context) != {"business_unit", "task", "connection_ids"}
            or (context["connection_ids"] is not None and (
                not isinstance(context["connection_ids"], list) or len(context["connection_ids"]) > 100))):
        raise GuidanceExportError("Guidance resolution context is required")
    for key in ("business_unit", "task"):
        if context[key] is not None:
            _text(context[key], "context " + key, maximum=240)
    for item in context["connection_ids"] or []:
        _text(item, "context source scope", maximum=240)
    evaluated_at = _date(resolution.get("evaluated_at"), "evaluated_at")
    if evaluated_at is None:
        raise GuidanceExportError("Guidance resolution evaluation time is required")
    bundles, slugs, rule_bindings = [], set(), {}
    for item in source:
        if (not isinstance(item, dict) or item.get("state") != "published"
                or item.get("dependency_state") != "current"
                or type(item.get("revision")) is not int or item["revision"] < 1
                or not isinstance(item.get("digest"), str) or not _DIGEST.fullmatch(item["digest"])):
            raise GuidanceExportError("Guidance needs a published revision/digest and current dependencies")
        if (item.get("use_as_instructions") is False or item.get("blocked_reasons")
                or item.get("published_revision", item["revision"]) != item["revision"]
                or (isinstance(item.get("publication"), dict) and item["publication"].get("revoked_at"))):
            raise GuidanceExportError("Revoked, blocked or superseded guidance cannot be exported")
        slug = _slug(item.get("slug"), "bundle slug")
        if slug in slugs:
            raise GuidanceExportError("Guidance bundle slugs collide")
        slugs.add(slug)
        payload = _payload(item.get("payload"))
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if hashlib.sha256(canonical.encode()).hexdigest() != item["digest"]:
            raise GuidanceExportError("Published guidance payload does not match its digest")
        evaluation = datetime.fromisoformat(evaluated_at)
        if ((payload.get("effective_from") and evaluation < datetime.fromisoformat(payload["effective_from"]))
                or (payload.get("effective_until") and evaluation >= datetime.fromisoformat(payload["effective_until"]))):
            raise GuidanceExportError("Guidance is outside its published validity interval")
        for rule in payload["rules"]:
            value = rule["value"]
            binding = (("number", str(Decimal(str(value)).normalize())) if type(value) in (int, float)
                       else (type(value).__name__, _json(value)))
            if rule["key"] in rule_bindings and rule_bindings[rule["key"]] != binding:
                raise GuidanceExportError("Conflicting company rules cannot be exported")
            rule_bindings[rule["key"]] = binding
        publication = item.get("publication")
        if publication is not None:
            if (not isinstance(publication, dict) or publication.keys() - {
                "published_at", "published_by", "rationale", "revoked_at", "revoked_by", "revoke_reason",
            }):
                raise GuidanceExportError("Invalid guidance publication provenance")
            _no_credentials(publication)
        bundles.append({"slug": slug, "revision": item["revision"], "digest": item["digest"],
                        "state": "published", "dependency_state": "current", "payload": payload,
                        "publication": deepcopy(publication), "created_at": item.get("created_at"),
                        "created_by": item.get("created_by"),
                        "validity": {key: payload.get(key) for key in ("effective_from", "effective_until")},
                        "dependencies": deepcopy(payload["dependencies"])})
    if sum(len(item["payload"]["skills"]) for item in bundles) > MAX_SKILLS:
        raise GuidanceExportError("Too many skills for a complete guidance export")
    manifest = {"schema_version": "knowledge-guidance-export-v1", "client": client,
                "enforcement": "guidance_only", "revalidate_before_use": True,
                "context": deepcopy(context), "evaluated_at": evaluated_at, "bundles": bundles,
                "required_capabilities": sorted({capability for item in bundles for skill in item["payload"]["skills"]
                                                 for capability in skill["required_capabilities"]})}
    _no_credentials(manifest)
    bindings = {**manifest, "bundles": [
        {**{key: value for key, value in item.items() if key != "payload"},
         "applicability": {key: item["payload"].get(key, []) for key in ("business_units", "tasks", "audience", "connection_ids")}}
        for item in bundles
    ]}
    references = (_BOOTSTRAP + "\n## Live registry bindings\n\n```json\n" + _json(bindings) + "\n```\n\n"
                  + _rules(bundles))
    files = {"knowledge-layer-guidance/BOOTSTRAP.md": references}
    for bundle in bundles:
        for skill in bundle["payload"]["skills"]:
            name = _name(bundle["slug"], skill["name"])
            root = f"{CLIENT_SKILLS[client]}/{name}"
            path = root + "/SKILL.md"
            if path in files:
                raise GuidanceExportError("Exported skill paths collide")
            header = ("---\nname: " + name + "\ndescription: " + _yaml_string(skill["description"])
                      + "\nmetadata:\n  knowledge-layer-bundle: " + json.dumps(bundle["slug"])
                      + '\n  knowledge-layer-revision: "' + str(bundle["revision"]) + '"'
                      + '\n  knowledge-layer-digest: "' + bundle["digest"] + '"'
                      + '\n  enforcement: "guidance_only"\n  revalidate-before-use: "true"\n---\n\n')
            files[path] = (header + "# " + skill["name"] + "\n\n"
                           + "Revalidate against the live registry before use; stop on stale, revoked or conflicting guidance.\n\n"
                           + "## Instructions\n\n" + skill["instructions"] + "\n\n"
                           + _rules(bundles) + "\n\n## Required capabilities (descriptive only)\n\n"
                           + _json(skill["required_capabilities"]) + "\n\n"
                           + "Read [company guidance and exact live registry bindings](references/company-guidance.md).\n")
            files[root + "/references/company-guidance.md"] = references
    manifest["files"] = {path: {"sha256": hashlib.sha256(content.encode()).hexdigest(),
                               "bytes": len(content.encode())} for path, content in files.items()}
    result = {"manifest": manifest, "files": files}
    if len(_json(result).encode()) > MAX_EXPORT_BYTES:
        raise GuidanceExportError("Complete guidance export exceeds the 256 KiB budget; narrow the applicable scope")
    return result
