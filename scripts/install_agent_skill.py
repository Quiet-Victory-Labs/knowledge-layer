"""Install the portable knowledge-layer skill into a chosen agent workspace."""

from __future__ import annotations

import argparse
import filecmp
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "knowledge-layer"
CLIENT_DIR = {"portable": ".agents", "codex": ".agents", "claude": ".claude", "cursor": ".cursor"}


def _no_symlink_ancestors(path: Path) -> None:
    """Do not install through a link into a different workspace."""
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"installation parent is a symlink: {parent}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"installation parent is not a directory: {parent}")


def _same_skill(source: Path, target: Path) -> bool:
    if not target.is_dir() or target.is_symlink():
        return False
    if any(path.is_symlink() for path in target.rglob("*")):
        return False
    source_files = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
    target_files = {path.relative_to(target) for path in target.rglob("*") if path.is_file()}
    return source_files == target_files and all(
        filecmp.cmp(source / relative, target / relative, shallow=False)
        for relative in source_files
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=sorted(CLIENT_DIR), required=True)
    parser.add_argument("--scope", choices=["project", "user"], default="project")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Target project root for project scope")
    parser.add_argument("--destination", type=Path,
                        help="Explicit full skill directory, ending in knowledge-layer; overrides client/scope placement")
    parser.add_argument("--replace", action="store_true", help="Back up and replace an existing different skill")
    args = parser.parse_args()
    base = args.project.absolute() if args.scope == "project" else Path.home()
    if args.destination is None and not base.is_dir():
        parser.error("the target project directory does not exist")
    target = (args.destination.absolute() if args.destination is not None
              else base / CLIENT_DIR[args.client] / "skills" / "knowledge-layer")
    if ".." in target.parts:
        parser.error("the destination must not contain parent traversal")
    if target.name != "knowledge-layer":
        parser.error("the destination must end in knowledge-layer to match the skill name")
    if not SOURCE.is_dir() or SOURCE.is_symlink() or any(path.is_symlink() for path in SOURCE.rglob("*")):
        parser.error("the source skill must be a directory without symlinks")
    source = SOURCE.absolute()
    if target == source or target in source.parents or source in target.parents:
        parser.error("the destination must not overlap the source skill")
    try:
        _no_symlink_ancestors(target.parent)
    except ValueError as exc:
        parser.error(str(exc))
    if _same_skill(SOURCE, target):
        print(f"Already current: {target}")
        return 0
    existing = target.exists() or target.is_symlink()
    if existing:
        if not args.replace:
            parser.error(f"{target} already exists; use --replace to preserve a backup and update it")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = target.with_name(f"{target.name}.backup-{stamp}")
        if backup.exists() or backup.is_symlink():
            parser.error(f"backup already exists: {backup}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Prepare a complete copy before moving the customer's existing directory.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".knowledge-layer-install-") as stage:
        prepared = Path(stage) / "knowledge-layer"
        shutil.copytree(SOURCE, prepared)
        if existing:
            target.rename(backup)
            print(f"Preserved previous skill: {backup}")
        try:
            prepared.rename(target)
        except OSError:
            if existing and not target.exists() and not target.is_symlink():
                backup.rename(target)
            raise
    print(f"Installed {args.client} skill: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
