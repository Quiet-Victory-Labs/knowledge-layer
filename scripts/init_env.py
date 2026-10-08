"""Create owner-only local credentials for the Compose evaluation profile."""

import os
import stat
from pathlib import Path
from secrets import token_urlsafe

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
REQUIRED = ("POSTGRES_PASSWORD", "KNOWLEDGE_AGENT_TOKEN")


def main() -> None:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(ENV_PATH, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(ENV_PATH, flags)
        created = False
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or details.st_nlink != 1:
            raise RuntimeError("Local credential path must be an owner-controlled regular file")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as env_file:
            descriptor = -1
            existing = env_file.read()
            present = {line.partition("=")[0] for line in existing.splitlines() if "=" in line}
            missing = [name for name in REQUIRED if name not in present]
            if missing:
                env_file.seek(0, os.SEEK_END)
                separator = "" if not existing or existing.endswith("\n") else "\n"
                env_file.write(separator + "".join(f"{name}={token_urlsafe(32)}\n" for name in missing))
                env_file.flush()
                os.fsync(env_file.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if created:
        print(f"Created local credentials at {ENV_PATH}")
    elif missing:
        print(f"Added missing local credentials to {ENV_PATH}")
    else:
        print(f"Existing {ENV_PATH} retained")


if __name__ == "__main__":
    main()
