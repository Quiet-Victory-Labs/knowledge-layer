"""Assemble a library-complete runtime from the pinned Debian/Python build stage.

This is deliberately not a CVE filter. Selected Debian packages keep their files,
copyrights and original dpkg status records for independent inventory/scanning.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import shutil
import ssl
import subprocess
import sys
from pathlib import Path


def command(arguments: list[str]) -> str:
    return subprocess.check_output(arguments, text=True, stderr=subprocess.STDOUT, timeout=30)


def linkage(output: str) -> set[Path]:
    if "not found" in output:
        raise ValueError("A native runtime library is missing")
    libraries = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "=>":
            candidate = fields[2]
        elif fields and fields[0].startswith("/"):
            candidate = fields[0]
        else:
            continue
        if candidate.startswith("/"):
            libraries.add(Path(candidate).resolve(strict=True))
    return libraries


def package_records(status: str, selected: set[str]) -> str:
    records = []
    for paragraph in status.strip().split("\n\n"):
        fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", paragraph, re.MULTILINE))
        name = fields.get("Package", "")
        identity = f"{name}:{fields.get('Architecture', '')}"
        if name in selected or identity in selected:
            if fields.get("Status") != "install ok installed":
                raise ValueError("Selected Debian package is not installed")
            records.append(paragraph)
    if len(records) != len(selected):
        raise ValueError("Selected Debian package inventory is incomplete")
    return "\n\n".join(records) + "\n"


def package_inventory(status: str) -> list[dict[str, str]]:
    """Keep binary/source identities separate for source-package CVE fanout."""
    result = []
    for paragraph in status.strip().split("\n\n"):
        fields = dict(re.findall(r"^([A-Za-z-]+): (.*)$", paragraph, re.MULTILINE))
        if fields.get("Status") != "install ok installed":
            raise ValueError("Runtime inventory requires installed package records")
        if any(not fields.get(name) for name in ("Package", "Version", "Architecture")):
            raise ValueError("Runtime package identity is incomplete")
        source = fields.get("Source", fields["Package"])
        match = re.fullmatch(r"([^ ()]+)(?: \(([^()]+)\))?", source)
        if match is None:
            raise ValueError("Runtime source package identity is invalid")
        result.append({"binary_package": fields["Package"],
                       "binary_version": fields["Version"],
                       "architecture": fields["Architecture"],
                       "source_package": match[1],
                       "source_version": match[2] or fields["Version"]})
    return sorted(result, key=lambda record: (record["binary_package"], record["architecture"]))


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def remove_unsupported_runtime_code(
    root: Path, *, drop_headless_terminal: bool = False
) -> list[dict[str, str]]:
    """Remove unsupported runtime code, preserving package evidence.

    The service neither installs packages at runtime nor implements POP mail.
    The Debian service also does not provide interactive terminal controls.
    Fixed paths avoid a CVE-based package filter; original distribution metadata,
    package databases and vendor SBOMs remain untouched for independent scanning.
    Validate all paths before deleting anything so an escaping symlink refuses.
    """
    resolved_root = root.resolve(strict=True)
    candidates: set[Path] = set()
    for relative in ("usr/local/lib/python3.14", "usr/lib/python3.14",
                     "app/.venv/lib/python3.14"):
        directory = root / relative
        candidates.update(directory / name for name in (
            "ensurepip", "site-packages/pip", "poplib.py", "poplib.pyc"))
        candidates.update((directory / "__pycache__").glob("poplib.*.pyc"))
        if drop_headless_terminal and relative == "usr/local/lib/python3.14":
            candidates.add(directory / "curses")
            for name in ("_curses", "_curses_panel", "readline"):
                candidates.update((directory / "lib-dynload").glob(f"{name}.*.so"))
    candidates.update((root / "usr/share/python-wheels").glob("pip-*.whl"))
    for relative in ("usr/local/bin", "usr/bin", "app/.venv/bin"):
        candidates.update(root / relative / name for name in ("pip", "pip3", "pip3.14"))
    retained = []
    selected = []
    for path in sorted(candidates):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(resolved_root):
            raise ValueError("Unsupported runtime-code path escapes the assembled root")
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for member in files:
            if member.is_symlink() or not member.resolve(strict=True).is_relative_to(resolved_root):
                raise ValueError("Unsupported runtime-code member escapes the assembled root")
            if member.is_file():
                retained.append({"path": "/" + str(member.relative_to(root)),
                                 "sha256": file_sha256(member)})
        selected.append(path)
    for path in selected:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return sorted(retained, key=lambda record: record["path"])


def apk_inventory(status: str) -> list[dict[str, str]]:
    result = []
    for paragraph in status.strip().split("\n\n"):
        fields = dict(re.findall(r"^([PVAoLc]):(.*)$", paragraph, re.MULTILINE))
        if any(not fields.get(name) for name in ("P", "V", "A", "o", "L", "c")):
            raise ValueError("Runtime APK package identity is incomplete")
        result.append({"binary_package": fields["P"], "binary_version": fields["V"],
                       "architecture": fields["A"], "source_package": fields["o"],
                       "source_commit": fields["c"], "license": fields["L"]})
    return sorted(result, key=lambda record: (record["binary_package"], record["architecture"]))


def verify_wolfi_runtime() -> dict:
    """Verify the final root, keeping original package and vendor SBOM identities."""
    if sys.version_info[:3] != (3, 14, 8):
        raise ValueError("The reviewed vendor CPython version changed")
    loaders = [path for path in Path("/usr/lib").glob("ld-linux-*.so.*") if path.is_file()]
    if len(loaders) != 1:
        raise ValueError("Runtime loader identity is ambiguous")
    libraries: set[Path] = set()
    elf_count = 0
    for directory in (Path("/usr/lib/python3.14"), Path("/app/.venv")):
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file() or ".so" not in path.name:
                continue
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            libraries.update(linkage(command([str(loaders[0]), "--list", str(path)])))
            libraries.add(path)
            elf_count += 1
    libraries.update(linkage(command([str(loaders[0]), "--list", sys.executable])))
    for name in ("psycopg", "uvloop", "httptools", "watchfiles", "rapidfuzz",
                 "_sqlite3", "_bz2", "_lzma", "_uuid", "_ctypes"):
        importlib.import_module(name)
    for name in ("pyarrow", "pyiceberg", "boto3", "s3fs", "clickhouse_connect",
                 "fastembed", "onnxruntime", "pyshacl", "google.auth"):
        parent = name.split(".", 1)[0]
        if importlib.util.find_spec(parent) is not None:
            importlib.import_module(name)
    if not ssl.create_default_context().get_ca_certs():
        raise ValueError("Runtime TLS trust store is empty")
    status_path = Path("/lib/apk/db/installed")
    packages = apk_inventory(status_path.read_text())
    if not list(Path("/var/lib/db/sbom").glob("*.spdx.json")):
        raise ValueError("Vendor package SBOM evidence is missing")
    from apply_runtime_patches import apply
    cleanup = apply(Path("/"), Path("/tmp/runtime-patches"))
    removed = remove_unsupported_runtime_code(Path("/"))
    for name in ("pip", "ensurepip", "poplib"):
        if importlib.util.find_spec(name) is not None:
            raise ValueError("Unsupported runtime installer or POP code remains")
    receipt = {"schema_version": "vendor-runtime-v1", "scanned_elf_files": elf_count,
               "apk_package_inventory": packages, "apk_status_sha256": file_sha256(status_path),
               "required_native_libraries": sorted(str(path) for path in libraries),
               "required_native_sha256": {str(path): file_sha256(path) for path in sorted(libraries)},
               "removed_runtime_code": removed,
               "temporary_cleanup_policy": cleanup,
               "cve_exemptions": []}
    report = Path("/usr/share/knowledge-layer/runtime-files.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def copy_path(source: Path, root: Path) -> None:
    destination = root / source.relative_to("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if not destination.is_symlink():
            destination.symlink_to(source.readlink())
    elif source.is_dir():
        destination.mkdir(exist_ok=True)
        destination.chmod(source.stat().st_mode & 0o777)
    elif source.is_file():
        shutil.copy2(source, destination)


def build(root: Path) -> dict:
    if root.exists():
        raise ValueError("Runtime destination must be new")
    root.mkdir()
    # Debian's /lib loader paths and usr-merged library package paths both work.
    for source in (Path("/lib"), Path("/lib64")):
        if source.is_symlink():
            copy_path(source, root)

    libraries: set[Path] = set()
    elf_count = 0
    for directory in (Path("/usr/local"), Path("/app/.venv")):
        for path in directory.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if ".so" not in path.name and not (
                path.parent == Path("/usr/local/bin") and path.name.startswith("python")
            ):
                continue
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            # The official slim image already omits Tcl/Tk's native libraries.
            # This headless platform never supports its GUI stdlib extension.
            if path.name.startswith("_tkinter."):
                continue
            if (path.parent == Path("/usr/local/lib/python3.14/lib-dynload")
                    and path.name.startswith(("_curses.", "_curses_panel.", "readline."))):
                continue
            libraries.update(linkage(command(["ldd", str(path)])))
            elf_count += 1

    selected = {"base-files", "ca-certificates", "netbase", "tzdata", "openssl",
                "openssl-provider-legacy"}
    for library in libraries:
        if library.is_relative_to("/usr/local") or library.is_relative_to("/app"):
            continue
        owners = command(["dpkg-query", "--search", str(library)]).strip().splitlines()
        if len(owners) != 1 or ": /" not in owners[0]:
            raise ValueError("Native runtime library ownership is ambiguous")
        selected.add(owners[0].split(": /", 1)[0])
    # libgcc's package/copyright dependency accompanies its native library.
    selected.add(command(["dpkg-query", "-W", "-f=${binary:Package}", "gcc-14-base"]).strip())

    status = package_records(Path("/var/lib/dpkg/status").read_text(), selected)
    for package in sorted(selected):
        for entry in command(["dpkg-query", "--listfiles", package]).splitlines():
            source = Path(entry)
            if entry in ("/", "/.") or not source.exists() and not source.is_symlink():
                continue
            copy_path(source, root)
        # File ownership and upstream source/version evidence survive trimming.
        for path in Path("/var/lib/dpkg/info").glob(f"{package}.*"):
            if path.suffix in (".list", ".md5sums", ".shlibs", ".symbols"):
                copy_path(path, root)
    (root / "var/lib/dpkg/status").write_text(status)
    for directory in ("/app", "/usr/local/lib"):
        shutil.copytree(directory, root / directory[1:], symlinks=True, dirs_exist_ok=True)
    for path in Path("/usr/local/bin").glob("python*"):
        if "config" not in path.name:
            copy_path(path, root)
    shutil.copytree("/etc/ssl", root / "etc/ssl", symlinks=True, dirs_exist_ok=True)
    for path in ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/localtime", "/etc/timezone"):
        if Path(path).exists():
            copy_path(Path(path), root)
    for directory in ("/etc/ld.so.conf.d",):
        shutil.copytree(directory, root / directory[1:], symlinks=True, dirs_exist_ok=True)
    (root / "etc/passwd").write_text(
        "root:x:0:0:root:/nonexistent:/nonexistent\n"
        "knowledge:x:10001:10001:Knowledge Layer:/nonexistent:/nonexistent\n")
    (root / "etc/group").write_text("root:x:0:\nknowledge:x:10001:\n")
    (root / "etc/nsswitch.conf").write_text(
        "passwd: files\ngroup: files\nhosts: files dns\nnetworks: files\n")
    (root / "tmp").mkdir(mode=0o1777, exist_ok=True)
    (root / "tmp").chmod(0o1777)
    from apply_runtime_patches import apply
    cleanup = apply(root, Path("/tmp/runtime-patches"))
    removed = remove_unsupported_runtime_code(root, drop_headless_terminal=True)
    if not all(any(item["path"].startswith(prefix) for item in removed)
               for prefix in ("/usr/local/lib/python3.14/curses/",
                              "/usr/local/lib/python3.14/lib-dynload/_curses.",
                              "/usr/local/lib/python3.14/lib-dynload/_curses_panel.",
                              "/usr/local/lib/python3.14/lib-dynload/readline.")):
        raise ValueError("Headless terminal code was not removed")
    if any(name in library.name for library in libraries
           for name in ("libncurses", "libtinfo", "libpanel", "libreadline")):
        raise ValueError("Headless terminal library is still required")
    receipt = {"schema_version": "minimal-runtime-v1", "scanned_elf_files": elf_count,
               "debian_packages": sorted(selected),
               "debian_package_inventory": package_inventory(status),
               "dpkg_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
               "required_native_libraries": sorted(str(path) for path in libraries),
               "required_native_sha256": {str(path): file_sha256(path)
                                          for path in sorted(libraries)},
               "unsupported_base_extension": "_tkinter (also unsupported by official slim base)",
               "removed_runtime_code": removed,
               "temporary_cleanup_policy": cleanup,
               "cve_exemptions": []}
    report = root / "usr/share/knowledge-layer/runtime-files.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(receipt, indent=2) + "\n")
    # Execute against the assembled filesystem, so a missing loader/TLS path
    # fails the image build rather than surfacing first at customer startup.
    probe = """
import ctypes, hashlib, importlib, importlib.util, json, pathlib, ssl, sys
assert sys.version_info[:3] == (3, 14, 8)
for name in ['pip', 'ensurepip', 'poplib', 'curses', '_curses', '_curses_panel', 'readline']:
    assert importlib.util.find_spec(name) is None
for name in ['psycopg', 'uvloop', 'httptools', 'watchfiles', 'rapidfuzz',
             '_sqlite3', '_bz2', '_lzma', '_uuid', '_ctypes']:
    importlib.import_module(name)
assert ssl.create_default_context().get_ca_certs()
receipt = json.loads(pathlib.Path('/usr/share/knowledge-layer/runtime-files.json').read_text())
assert hashlib.sha256(pathlib.Path('/var/lib/dpkg/status').read_bytes()).hexdigest() == receipt['dpkg_status_sha256']
for path in receipt['required_native_libraries']:
    with open(path, 'rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == receipt['required_native_sha256'][path]
    if '/lib-dynload/' not in path and not path.startswith('/app/'):
        ctypes.CDLL(path)
"""
    command(["chroot", str(root), "/app/.venv/bin/python", "-B", "-c", probe])
    return receipt


if __name__ == "__main__":
    if sys.argv[1:] == ["--verify-wolfi"]:
        result = verify_wolfi_runtime()
        print(json.dumps({"packages": len(result["apk_package_inventory"]),
                          "elf_files": result["scanned_elf_files"]}))
        raise SystemExit(0)
    if len(sys.argv) != 2:
        raise SystemExit("Usage: build_runtime_root.py NEW_ABSOLUTE_ROOT")
    target = Path(sys.argv[1])
    if not target.is_absolute() or target == Path("/"):
        raise SystemExit("Runtime destination must be an absolute new directory")
    result = build(target)
    print(json.dumps({"packages": len(result["debian_packages"]),
                      "elf_files": result["scanned_elf_files"]}))
