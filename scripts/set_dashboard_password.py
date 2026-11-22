"""Set the dashboard password used by the Argon2id login form.

The password is stored as plaintext in the root-only environment file (that is
how systemd services are configured) and hashed in memory with Argon2id at
startup. Nothing password-derived is ever written to the dashboard database.

Examples:
    sudo python scripts/set_dashboard_password.py                # random 20 chars
    sudo python scripts/set_dashboard_password.py --password '...'
    python scripts/set_dashboard_password.py --print-only        # generate & show
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ENV_FILE = Path("/etc/opencode-proxy.env")
KEY = "DASHBOARD_PASSWORD"


def generate(length: int = 14) -> str:
    # Readable but high entropy (token_urlsafe: ~6 bits per char).
    return "oracle-" + secrets.token_urlsafe(length)


def update_env(path: Path, password: str) -> bool:
    lines: list[str] = []
    replaced = False
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(KEY + "=") or stripped.startswith(
                "DASHBOARD_PASSWORD_HASH="
            ):
                if not replaced and stripped.startswith(KEY + "="):
                    lines.append(f"{KEY}={password}")
                    replaced = True
                continue
            lines.append(line)
    if not replaced:
        lines.append(f"{KEY}={password}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    return replaced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", help="Password to set (default: random)")
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument(
        "--print-only", action="store_true", help="Print a generated password and exit"
    )
    args = parser.parse_args(argv)

    password = args.password or generate()
    if len(password) < 12:
        print("Refusing: use at least 12 characters.", file=sys.stderr)
        return 2

    if args.print_only:
        print(password)
        return 0

    if not args.env_file.exists():
        print(f"Env file not found: {args.env_file}", file=sys.stderr)
        return 2

    update_env(args.env_file, password)
    print(f"Dashboard password written to {args.env_file}")
    print(f"New password: {password}")
    print("Restart the proxy: sudo systemctl restart opencode-proxy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
