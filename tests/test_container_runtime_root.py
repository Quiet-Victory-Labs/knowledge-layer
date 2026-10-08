from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def runtime_root():
    path = Path(__file__).resolve().parents[1] / "scripts/build_runtime_root.py"
    spec = importlib.util.spec_from_file_location("build_runtime_root", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_missing_native_dependency_refuses_runtime_assembly(runtime_root):
    with pytest.raises(ValueError, match="missing"):
        runtime_root.linkage("libpq.so.5 => not found\n")


def test_linker_and_native_library_paths_both_survive(runtime_root, tmp_path):
    library = tmp_path / "libpq.so.5"
    loader = tmp_path / "ld-linux.so.1"
    library.touch()
    loader.touch()
    assert runtime_root.linkage(
        f"libpq.so.5 => {library} (0x1)\n{loader} (0x2)\nlinux-vdso.so.1 (0x3)\n"
    ) == {library, loader}


def test_kept_package_evidence_retains_source_and_original_status(runtime_root):
    status = (
        "Package: retained-lib\nStatus: install ok installed\nArchitecture: arm64\n"
        "Source: original-upstream (2.1)\nVersion: 2.1-3\n\n"
        "Package: unused-tool\nStatus: install ok installed\nArchitecture: arm64\n"
        "Version: 1.0\n"
    )
    retained = runtime_root.package_records(status, {"retained-lib:arm64"})
    assert "Source: original-upstream (2.1)" in retained
    assert "Version: 2.1-3" in retained
    assert "Status: install ok installed" in retained
    assert "unused-tool" not in retained
    with pytest.raises(ValueError, match="incomplete"):
        runtime_root.package_records(status, {"missing-lib"})
    with pytest.raises(ValueError, match="not installed"):
        runtime_root.package_records(status.replace("install ok installed", "deinstall ok config-files"),
                                     {"retained-lib:arm64"})


def test_docker_runtime_preserves_frozen_extras_and_omits_build_stage_tools():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "FROM scratch AS runtime" in dockerfile
    assert 'uv sync --frozen --no-dev --link-mode=copy "$@"' in dockerfile
    assert "iceberg|objects|clickhouse|snowflake|bigquery|drive|search|semantic-validation" in dockerfile
    assert '*) echo "Unknown runtime extra"' in dockerfile
    assert "COPY --from=runtime-build /runtime-root /" in dockerfile


def test_runtime_inventory_distinguishes_source_and_binary_versions(runtime_root):
    status = (
        "Package: zlib1g\nStatus: install ok installed\nArchitecture: arm64\n"
        "Source: zlib (1:1.3.1-1)\nVersion: 1:1.3.1-1+b1\n\n"
        "Package: libc6\nStatus: install ok installed\nArchitecture: arm64\n"
        "Source: glibc\nVersion: 2.41-12+deb13u4\n"
    )
    assert runtime_root.package_inventory(status) == [
        {"binary_package": "libc6", "binary_version": "2.41-12+deb13u4",
         "architecture": "arm64", "source_package": "glibc", "source_version": "2.41-12+deb13u4"},
        {"binary_package": "zlib1g", "binary_version": "1:1.3.1-1+b1",
         "architecture": "arm64", "source_package": "zlib", "source_version": "1:1.3.1-1"},
    ]
    with pytest.raises(ValueError, match="incomplete"):
        runtime_root.package_inventory(status.replace("Version: 1:1.3.1-1+b1\n", ""))
    with pytest.raises(ValueError, match="invalid"):
        runtime_root.package_inventory(status.replace("Source: glibc", "Source: glibc (broken"))
    with pytest.raises(ValueError, match="installed"):
        runtime_root.package_inventory(status.replace("install ok installed", "deinstall ok config-files"))


def test_package_without_source_field_keeps_binary_identity(runtime_root):
    assert runtime_root.package_inventory(
        "Package: netbase\nStatus: install ok installed\nArchitecture: all\nVersion: 6.5\n"
    ) == [{"binary_package": "netbase", "binary_version": "6.5", "architecture": "all",
           "source_package": "netbase", "source_version": "6.5"}]


def test_apk_inventory_preserves_vendor_version_commit_and_license(runtime_root):
    record = "P:glibc-2.44\nV:2.44-r7\nA:aarch64\no:glibc\nL:LGPL-2.1-or-later\nc:abcdef\n"
    assert runtime_root.apk_inventory(record) == [
        {"binary_package": "glibc-2.44", "binary_version": "2.44-r7", "architecture": "aarch64",
         "source_package": "glibc", "source_commit": "abcdef", "license": "LGPL-2.1-or-later"}]
    with pytest.raises(ValueError, match="incomplete"):
        runtime_root.apk_inventory(record.replace("c:abcdef\n", ""))


def test_optional_vendor_profile_keeps_frozen_inputs_and_runtime_evidence():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.chainguard").read_text()
    assert 'uv sync --python /usr/bin/python3.14 --frozen --no-dev --link-mode=copy "$@"' in dockerfile
    assert "iceberg|objects|clickhouse|bigquery|drive|search|semantic-validation" in dockerfile
    assert 'snowflake) echo "Snowflake requires an upstream OpenSSL 4 compatible adapter"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "--verify-wolfi" in dockerfile
    assert "COPY --from=runtime-build /app /app" in dockerfile
    assert "FROM scratch" not in dockerfile


def test_native_file_hash_changes_when_retained_binary_changes(runtime_root, tmp_path):
    library = tmp_path / "lib.so"
    library.write_bytes(b"reviewed native bytes")
    expected = runtime_root.file_sha256(library)
    assert expected == "aa42d10e1977114a65898a9b3af2cfadad559cd499bafca0e35b4f245f9d0b9a"
    library.write_bytes(b"replacement native bytes")
    assert runtime_root.file_sha256(library) != expected


def test_unsupported_code_removal_keeps_metadata_and_records_actual_hashes(runtime_root, tmp_path):
    library = tmp_path / "usr/local/lib/python3.14"
    paths = [library / "ensurepip/__init__.py", library / "ensurepip/_bundled/pip-1.whl",
             library / "site-packages/pip/__init__.py", library / "poplib.py",
             library / "__pycache__/poplib.cpython-314.pyc",
             tmp_path / "usr/share/python-wheels/pip-2.whl"]
    metadata = [library / "site-packages/pip-1.dist-info/METADATA",
                tmp_path / "var/lib/dpkg/status", tmp_path / "lib/apk/db/installed",
                tmp_path / "var/lib/db/sbom/python.spdx.json"]
    legitimate = library / "site-packages/service/__init__.py"
    for path in paths + metadata + [legitimate]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(str(path.relative_to(tmp_path)).encode())
    expected = [{"path": "/" + str(path.relative_to(tmp_path)),
                 "sha256": runtime_root.file_sha256(path)} for path in sorted(paths)]
    hashes = {path: runtime_root.file_sha256(path) for path in metadata + [legitimate]}
    assert runtime_root.remove_unsupported_runtime_code(tmp_path) == expected
    assert all(not path.exists() for path in paths)
    assert all(runtime_root.file_sha256(path) == digest for path, digest in hashes.items())
    assert runtime_root.remove_unsupported_runtime_code(tmp_path) == []


def test_headless_terminal_removal_records_exact_files_and_keeps_inventory(runtime_root, tmp_path):
    library = tmp_path / "usr/local/lib/python3.14"
    paths = [library / "curses/__init__.py",
             library / "curses/__pycache__/__init__.cpython-314.pyc",
             library / "lib-dynload/_curses.cpython-314-x86_64-linux-gnu.so",
             library / "lib-dynload/_curses_panel.cpython-314-x86_64-linux-gnu.so",
             library / "lib-dynload/readline.cpython-314-x86_64-linux-gnu.so"]
    inventory = tmp_path / "var/lib/dpkg/status"
    unrelated = tmp_path / "app/.venv/lib/python3.14/site-packages/readline.cpython-314-x86_64-linux-gnu.so"
    for path in paths + [inventory, unrelated]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(str(path.relative_to(tmp_path)).encode())
    inventory_hash = runtime_root.file_sha256(inventory)
    unrelated_hash = runtime_root.file_sha256(unrelated)
    removed = runtime_root.remove_unsupported_runtime_code(tmp_path, drop_headless_terminal=True)
    assert {item["path"] for item in removed} == {
        "/" + str(path.relative_to(tmp_path)) for path in paths
    }
    assert all(not path.exists() for path in paths)
    assert runtime_root.file_sha256(inventory) == inventory_hash
    assert runtime_root.file_sha256(unrelated) == unrelated_hash


def test_code_removal_refuses_escaping_alias_before_mutation(runtime_root, tmp_path):
    assembled = tmp_path / "root"
    library = assembled / "usr/local/lib/python3.14"
    (library / "ensurepip").mkdir(parents=True)
    retained = library / "ensurepip/__init__.py"
    retained.write_text("installer marker")
    outside = tmp_path / "outside.py"
    outside.write_text("must survive")
    (library / "poplib.py").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        runtime_root.remove_unsupported_runtime_code(assembled)
    assert retained.exists() and outside.read_text() == "must survive"


def test_code_removal_refuses_nested_alias(runtime_root, tmp_path):
    library = tmp_path / "usr/lib/python3.14/ensurepip"
    library.mkdir(parents=True)
    (library / "payload.py").symlink_to(tmp_path / "unrelated.py")
    with pytest.raises(ValueError, match="member escapes"):
        runtime_root.remove_unsupported_runtime_code(tmp_path)
    assert (library / "payload.py").is_symlink()


CONFIGURE_SMOKE = r'''
import importlib.util, json, pathlib, sys, tempfile
root = pathlib.Path('/reviewed')
spec = importlib.util.spec_from_file_location('configuration', root/'scripts/configure_install.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as temporary:
    target = pathlib.Path(temporary)
    settings = json.loads((root/'deploy/settings.example.json').read_text())
    for name in ['image', 'postgres_image', 'proxy_image']:
        settings[name] = settings[name].split('@', 1)[0] + '@sha256:' + 'a'*64
    settings_path = target/'settings.json'
    settings_path.write_text(json.dumps(settings))
    secret = target/'client-secret'
    secret.write_text('synthetic-runtime-configuration-marker')
    secret.chmod(0o600)
    policy = target/'policy'
    policy.write_text(json.dumps({'version': 1, 'sources': []}))
    policy.chmod(0o600)
    output = target/'installation'
    try:
        module.configure(settings_path, secret, policy, output, 10001, 10001)
    except PermissionError:
        assert sys.argv[1] == 'refusal'
        assert all(p.stat().st_mode & 0o077 == 0 for p in output.rglob('*') if p.is_file())
        print(json.dumps({'result': 'refused_without_chown'}))
    else:
        assert sys.argv[1] == 'success'
        files = [p for p in output.rglob('*') if p.is_file()]
        assert len(files) == 8
        assert all(p.stat().st_mode & 0o077 == 0 for p in files)
        assert all(p.stat().st_uid == p.stat().st_gid == 10001
                   for p in (output/'secrets').iterdir())
        print(json.dumps({'result': 'passed_with_only_chown', 'generated_files': len(files)}))
'''


@pytest.mark.skipif(not os.environ.get("KNOWLEDGE_RUNTIME_TEST_IMAGE"),
                    reason="Explicit local image needed for offline container controls")
@pytest.mark.parametrize("add_chown", [False, True])
def test_offline_configuration_needs_only_chown_capability(add_chown):
    repository = Path(__file__).resolve().parents[1]
    arguments = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
                 "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--memory", "384m", "--cpus", "1", "--pids-limit", "64",
                 "--tmpfs", "/tmp:size=64m", "--mount",
                 f"type=bind,src={repository},dst=/reviewed,readonly"]
    if add_chown:
        arguments += ["--cap-add", "CHOWN"]
    arguments += ["--entrypoint", "python", os.environ["KNOWLEDGE_RUNTIME_TEST_IMAGE"],
                  "-", "success" if add_chown else "refusal"]
    result = subprocess.run(arguments, input=CONFIGURE_SMOKE, text=True, capture_output=True,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["result"] == (
        "passed_with_only_chown" if add_chown else "refused_without_chown")


NATIVE_SMOKE = r'''
import ctypes, hashlib, importlib, io, json, os, pathlib, ssl, subprocess, sys, tempfile, venv, zlib
import importlib.util
receipt = json.loads(pathlib.Path('/usr/share/knowledge-layer/runtime-files.json').read_text())
cleanup = receipt['temporary_cleanup_policy']
assert cleanup['policy'] == 'descriptor_safe_cleanup_without_permission_repair'
assert cleanup['package_versions_changed'] is False and cleanup['cve_disposition'] == 'unresolved'
assert cleanup['modified_sources']
for item in cleanup['modified_sources']:
    assert hashlib.sha256(pathlib.Path(item['path']).read_bytes()).hexdigest() == item['sha256']
for item in cleanup['removed_bytecode']:
    assert not pathlib.Path(item['path']).exists()
if 'debian_package_inventory' in receipt:
    status = pathlib.Path('/var/lib/dpkg/status').read_bytes()
    assert hashlib.sha256(status).hexdigest() == receipt['dpkg_status_sha256']
    assert len(receipt['debian_package_inventory']) == len(receipt['debian_packages'])
    terminal_packages = {'libncursesw6', 'libtinfo6', 'libpanelw6', 'libreadline8'}
    assert not terminal_packages.intersection(
        item['binary_package'] for item in receipt['debian_package_inventory'])
    assert not any(any(name in path for name in ('libncurses', 'libtinfo', 'libpanel', 'libreadline'))
                   for path in receipt['required_native_libraries'])
    for name in ['curses', '_curses', '_curses_panel', 'readline']:
        assert importlib.util.find_spec(name) is None
    dynamic = pathlib.Path('/usr/local/lib/python3.14/lib-dynload')
    assert all(not any(dynamic.glob(f'{name}.*.so'))
               for name in ['_curses', '_curses_panel', 'readline'])
    package_count = len(receipt['debian_package_inventory'])
else:
    status = pathlib.Path('/lib/apk/db/installed').read_bytes()
    assert hashlib.sha256(status).hexdigest() == receipt['apk_status_sha256']
    assert list(pathlib.Path('/var/lib/db/sbom').glob('*.spdx.json'))
    package_count = len(receipt['apk_package_inventory'])
for name, expected in receipt['required_native_sha256'].items():
    with open(name, 'rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected
assert receipt['removed_runtime_code']
for item in receipt['removed_runtime_code']:
    assert len(item['sha256']) == 64 and not pathlib.Path(item['path']).exists()
for name in ['pip', 'ensurepip', 'poplib']:
    assert importlib.util.find_spec(name) is None
system_python = str(pathlib.Path(sys.base_prefix)/'bin/python3.14')
control = "import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ['pip','ensurepip','poplib'])"
assert subprocess.run([system_python, '-I', '-c', control], timeout=20).returncode == 0
with tempfile.TemporaryDirectory() as temporary:
    # The tmpfs stays noexec; the venv uses the approved system binary via a link.
    venv.EnvBuilder(with_pip=False, symlinks=True).create(pathlib.Path(temporary)/'venv')
    assert subprocess.run([str(pathlib.Path(temporary)/'venv/bin/python'), '-I', '-c', control],
                          timeout=20).returncode == 0
for name in ['psycopg', 'uvloop', 'httptools', 'watchfiles', 'rapidfuzz',
             '_sqlite3', '_bz2', '_lzma', '_uuid', '_ctypes']:
    importlib.import_module(name)
assert os.getuid() == os.getgid() == 10001
assert ssl.create_default_context().get_ca_certs()
assert zlib.decompress(zlib.compress(b'legitimate compressed document')) == b'legitimate compressed document'
absent_tools = ['mount', 'nsenter', 'sh', 'apk', 'apt-get', 'pip', 'uv']
if 'debian_package_inventory' in receipt:
    absent_tools.append('infocmp')
for name in absent_tools:
    assert not any(p.name == name for directory in ['/usr/bin', '/usr/sbin', '/bin', '/sbin']
                   if pathlib.Path(directory).exists() for p in pathlib.Path(directory).iterdir())
# Call the real C++ nothrow aligned allocator: wrapped sizes must refuse while a
# small, correctly aligned allocation remains usable. No C++ exception crosses FFI.
cxx = ctypes.CDLL('libstdc++.so.6')
allocate = cxx._ZnwmSt11align_val_tRKSt9nothrow_t
allocate.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p]
allocate.restype = ctypes.c_void_p
release = cxx._ZdlPvSt11align_val_t
release.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
release.restype = None
nothrow = ctypes.byref(ctypes.c_char.in_dll(cxx, '_ZSt7nothrow'))
maximum = ctypes.c_size_t(-1).value
cases = [(maximum - 8, 32), (maximum, 8), (maximum, 32), (maximum, 1024),
         (maximum, 65536), (maximum - 1, 16), (maximum - 1025, 1024),
         (maximum - 1024, 1024), (maximum - 65536, 65536)]
for size, alignment in cases:
    assert allocate(size, alignment, nothrow) is None
pointer = allocate(128, 64, nothrow)
assert pointer is not None and pointer % 64 == 0
release(pointer, 64)
extras = sys.argv[1] == 'extras'
if extras:
    optional_modules = ['pyarrow', 'pyarrow.parquet', 'pyiceberg', 'boto3', 's3fs',
                        'clickhouse_connect', 'fastembed', 'onnxruntime', 'pyshacl', 'google.auth']
    if 'debian_package_inventory' in receipt:
        optional_modules.append('snowflake.connector')
    else:
        assert importlib.util.find_spec('snowflake') is None
    for name in optional_modules:
        importlib.import_module(name)
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.table({'id': [1, 2], 'value': [None, 0]})
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    assert pq.read_table(pa.BufferReader(buffer.getvalue())).equals(table)
    import onnxruntime
    assert 'CPUExecutionProvider' in onnxruntime.get_available_providers()
    import numpy as np
    values = np.array([[1.0, 0.0]], dtype=np.float32)
    assert np.array_equal(onnxruntime.OrtValue.ortvalue_from_numpy(values).numpy(), values)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = ec.generate_private_key(ec.SECP256R1())
    signature = key.sign(b'offline native control', ec.ECDSA(hashes.SHA256()))
    key.public_key().verify(signature, b'offline native control', ec.ECDSA(hashes.SHA256()))
    cipher = AESGCM(AESGCM.generate_key(bit_length=128))
    nonce = bytes(range(12))
    encrypted = cipher.encrypt(nonce, b'legitimate document', b'context')
    assert cipher.decrypt(nonce, encrypted, b'context') == b'legitimate document'
    # Exercise the host TLS library with an actual certificate and encrypted
    # exchange, using memory BIOs so there is no listener or outbound network.
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'runtime-control.invalid')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName('runtime-control.invalid')]), False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
                   .sign(key, hashes.SHA256()))
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    with tempfile.TemporaryDirectory() as temporary:
        directory = pathlib.Path(temporary)
        (directory/'cert.pem').write_bytes(pem)
        (directory/'key.pem').write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(directory/'cert.pem', directory/'key.pem')
        client_context = ssl.create_default_context(cadata=pem.decode())
        client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
        client = client_context.wrap_bio(client_in, client_out, server_hostname='runtime-control.invalid')
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        completed = [False, False]
        for attempt in range(12):
            for index, connection in enumerate([client, server]):
                try:
                    connection.do_handshake()
                    completed[index] = True
                except ssl.SSLWantReadError:
                    pass
            server_in.write(client_out.read())
            client_in.write(server_out.read())
            if all(completed):
                break
        assert all(completed)
        assert client.version() in ['TLSv1.2', 'TLSv1.3']
        assert client.write(b'legitimate encrypted document') == len(b'legitimate encrypted document')
        server_in.write(client_out.read())
        assert server.read() == b'legitimate encrypted document'
    import psycopg
    assert psycopg.pq.version() >= 150000
    from pyshacl import validate
    from rdflib import Graph
    data = Graph().parse(data='@prefix ex: <https://example.invalid/> . ex:a ex:value 1 .', format='turtle')
    shapes = Graph().parse(data=''' + "'''" + r'''
@prefix ex: <https://example.invalid/> . @prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:Shape a sh:NodeShape; sh:targetNode ex:a;
sh:property [ sh:path ex:value; sh:datatype xsd:integer; sh:minCount 1 ].
''' + "'''" + r''', format='turtle')
    assert validate(data, shacl_graph=shapes)[0]
print(json.dumps({'result': 'passed', 'uid': os.getuid(), 'native_hashes': len(receipt['required_native_sha256']),
                  'package_records': package_count,
                  'aligned_new_overflow_refusals': len(cases), 'tls_trust': 'passed',
                  'installer_and_pop_code_absent': 'passed',
                  'tls_memory_bio_exchange': 'passed' if extras else 'not_requested',
                  'parquet_shacl_optional_imports': 'passed' if extras else 'not_requested', 'model_calls': 0}))
'''


@pytest.mark.parametrize('environment,mode', [
    ('KNOWLEDGE_RUNTIME_TEST_IMAGE', 'base'), ('KNOWLEDGE_RUNTIME_EXTRAS_TEST_IMAGE', 'extras')])
def test_offline_runtime_native_tls_and_optional_document_controls(environment, mode):
    image = os.environ.get(environment)
    if not image:
        pytest.skip('Explicit local image needed for offline runtime controls')
    result = subprocess.run(
        ['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only', '--user', '10001:10001',
         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--memory', '512m', '--cpus', '1',
         '--pids-limit', '64', '--tmpfs', '/tmp:size=64m', '--entrypoint', 'python', image, '-', mode],
        input=NATIVE_SMOKE, text=True, capture_output=True, timeout=90, check=False)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['result'] == 'passed'
    assert receipt['aligned_new_overflow_refusals'] == 9
    assert receipt['model_calls'] == 0
