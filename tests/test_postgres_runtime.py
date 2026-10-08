from __future__ import annotations

import os
import subprocess

import pytest


def container_arguments(uid: int, gid: int) -> list[str]:
    return ["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
            "--user", f"{uid}:{gid}", "--cap-drop", "ALL", "--security-opt",
            "no-new-privileges", "--memory", "384m", "--cpus", "1", "--pids-limit", "64",
            "--shm-size", "64m", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m,mode=1777",
            "--tmpfs", f"/var/lib/postgresql:rw,noexec,nosuid,size=160m,uid={uid},gid={gid},mode=0700",
            "--tmpfs", f"/var/run/postgresql:rw,noexec,nosuid,size=8m,uid={uid},gid={gid},mode=0700"]


@pytest.mark.skipif(not os.environ.get("KNOWLEDGE_POSTGRES_TEST_IMAGE"),
                    reason="Explicit local PostgreSQL derivative image needed")
def test_postgres_entrypoint_refuses_root_before_upstream_execution():
    result = subprocess.run(container_arguments(0, 0) + [
        os.environ["KNOWLEDGE_POSTGRES_TEST_IMAGE"], "postgres", "--version"],
        text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 78, result.stderr
    assert "requires a nonroot numeric UID" in result.stderr
    assert "PostgreSQL)" not in result.stdout


POSTGRES_SMOKE = r'''
set -eu
expected_uid="$1"
expected_gid="$2"
test "$(id -u)" = "$expected_uid"
test "$(id -g)" = "$expected_gid"
test ! -e /usr/local/bin/gosu
test "$(awk '/^CapEff:/ { print $2 }' /proc/self/status)" = 0000000000000000
test "$(awk '/^NoNewPrivs:/ { print $2 }' /proc/self/status)" = 1
if touch /etc/knowledge-runtime-write-probe 2>/dev/null; then exit 1; fi
receipt=/usr/share/knowledge-layer/postgres-runtime.txt
test "$(sed -n 's/^apk_status_sha256=//p' "$receipt")" = "$(sha256sum /lib/apk/db/installed | cut -d ' ' -f 1)"
test "$(sed -n 's/^base_apk_status_sha256=//p' "$receipt")" = "$(sha256sum /usr/share/knowledge-layer/postgres-base-apk-installed.txt | cut -d ' ' -f 1)"
test "$(sed -n 's/^updated_package=//p' "$receipt")" = nghttp2-libs=1.70.0-r0
apk info -e 'nghttp2-libs=1.70.0-r0' >/dev/null
test "$(sed -n 's/^patched_package=//p' "$receipt")" = zlib=1.3.2-r1
apk info -e 'zlib=1.3.2-r1' >/dev/null
test "$(sed -n 's/^patched_libcrypto=//p' "$receipt")" = libcrypto3=3.5.9-r0
test "$(sed -n 's/^patched_libssl=//p' "$receipt")" = libssl3=3.5.9-r0
apk info -e 'libcrypto3=3.5.9-r0' >/dev/null
apk info -e 'libssl3=3.5.9-r0' >/dev/null
test "$(sed -n 's/^upstream_entrypoint_sha256=//p' "$receipt")" = "$(sha256sum /usr/local/bin/docker-entrypoint.sh | cut -d ' ' -f 1)"
test "$(sed -n 's/^removed_path=//p' "$receipt")" = /usr/local/bin/gosu
test "$(sed -n 's/^removed_sha256=//p' "$receipt" | wc -c)" = 65
printf '%s\n' 'synthetic-offline-postgres-marker' > /tmp/database-password
chmod 600 /tmp/database-password
export POSTGRES_PASSWORD_FILE=/tmp/database-password
export POSTGRES_USER=knowledge_layer POSTGRES_DB=knowledge_layer
socket=/var/run/postgresql
/usr/local/bin/knowledge-postgres-entrypoint postgres -c listen_addresses= -c shared_buffers=16MB -c max_connections=10 > /tmp/postgres.log 2>&1 &
server_pid=$!
cleanup() { kill -TERM "$server_pid" 2>/dev/null || :; wait "$server_pid" 2>/dev/null || :; }
trap cleanup EXIT HUP INT TERM
ready=0
for attempt in $(seq 1 60); do
    if psql -h "$socket" -U knowledge_layer -d knowledge_layer -Atqc 'SELECT 1' > /tmp/ready 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then cat /tmp/postgres.log >&2; exit 1; fi
    sleep 0.25
done
if [ "$ready" -ne 1 ]; then cat /tmp/postgres.log >&2; exit 1; fi
test "$(cat "$PGDATA/PG_VERSION")" = 18
test "$(stat -c %u "$PGDATA")" = "$expected_uid"
test "$(stat -c %a "$PGDATA")" = 700
psql -h "$socket" -U knowledge_layer -d knowledge_layer -v ON_ERROR_STOP=1 -Atq <<'SQL' > /tmp/results
CREATE TABLE qualification (id integer PRIMARY KEY, value text NOT NULL);
INSERT INTO qualification VALUES (1, 'bounded offline roundtrip');
SELECT value FROM qualification WHERE id = 1;
SHOW listen_addresses;
SQL
test "$(head -n 1 /tmp/results)" = 'bounded offline roundtrip'
test -z "$(tail -n 1 /tmp/results)"
pg_dump -h "$socket" -U knowledge_layer -d knowledge_layer -Fc -Z gzip:1 > /tmp/qualification.dump
psql -h "$socket" -U knowledge_layer -d knowledge_layer -v ON_ERROR_STOP=1 -qc 'DROP TABLE qualification'
pg_restore -h "$socket" -U knowledge_layer -d knowledge_layer --exit-on-error /tmp/qualification.dump
test "$(psql -h "$socket" -U knowledge_layer -d knowledge_layer -Atqc 'SELECT value FROM qualification WHERE id=1')" = 'bounded offline roundtrip'
grep -q 'PostgreSQL init process complete' /tmp/postgres.log
printf 'passed uid=%s gid=%s postgres=%s rootfs=readonly caps=none network=none\n' "$expected_uid" "$expected_gid" "$(cat "$PGDATA/PG_VERSION")"
'''


@pytest.mark.skipif(not os.environ.get("KNOWLEDGE_POSTGRES_TEST_IMAGE"),
                    reason="Explicit local PostgreSQL derivative image needed")
@pytest.mark.parametrize("uid,gid", [(10001, 10001), (501, 20)])
def test_postgres_initializes_and_queries_as_arbitrary_numeric_uid(uid: int, gid: int):
    arguments = container_arguments(uid, gid) + [
        "--entrypoint", "/usr/local/bin/knowledge-postgres-entrypoint",
        os.environ["KNOWLEDGE_POSTGRES_TEST_IMAGE"], "sh", "-s", "--", str(uid), str(gid)]
    result = subprocess.run(arguments, input=POSTGRES_SMOKE, text=True,
                            capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert f"passed uid={uid} gid={gid} postgres=18" in result.stdout
