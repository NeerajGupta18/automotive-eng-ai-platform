"""Operations CLI (works on the data directory given by DATA_DIR; run it with the service stopped for restore).

  python scripts/manage.py check-config
  python scripts/manage.py create-user alice --role user            # prints the token once
  python scripts/manage.py grant demo alice engineer
  python scripts/manage.py verify-audit                              # exit 1 if the audit chain is broken
  python scripts/manage.py backup /backups/platform-$(date +%F).tar.gz
  python scripts/manage.py restore /backups/platform-2026-09-28.tar.gz --force
  python scripts/manage.py reindex [--project demo]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings, validate  # noqa: E402
from app.core import maintenance  # noqa: E402
from app.core.auth import AuthService  # noqa: E402
from app.core.db import Database  # noqa: E402
from app.core.errors import Conflict, Invalid, NotFound  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config")
    sub.add_parser("migrate")
    sub.add_parser("verify-audit")
    sub.add_parser("list-users")
    p = sub.add_parser("create-user"); p.add_argument("name"); p.add_argument("--role", default="user", choices=["user", "admin"]); p.add_argument("--ttl-days", type=int)
    for c in ("rotate", "disable", "enable"):
        p = sub.add_parser(c); p.add_argument("name")
    p = sub.add_parser("grant"); p.add_argument("project"); p.add_argument("user"); p.add_argument("role")
    p = sub.add_parser("backup"); p.add_argument("out")
    p = sub.add_parser("restore"); p.add_argument("archive"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("reindex"); p.add_argument("--project")
    a = ap.parse_args()
    s = get_settings()
    errors, warnings = validate(s)
    if a.cmd == "check-config":
        for w in warnings:
            print("WARNING:", w)
        for e in errors:
            print("ERROR:", e)
        print(f"auth={s.auth_mode} llm={s.llm_backend} embed={s.embed_backend} vector={s.vector_backend} data={s.data_dir}")
        return 1 if errors else 0
    try:
        if a.cmd == "restore":
            print(json.dumps(maintenance.restore(Path(a.archive), s.data_dir, a.force), indent=1))
            return 0
        if a.cmd == "backup":
            print(json.dumps(maintenance.backup(s.data_dir, Path(a.out)), indent=1))
            return 0
        if a.cmd == "reindex":
            from app.container import Container
            print(json.dumps(maintenance.reindex(Container(s), a.project), indent=1))
            return 0
        db = Database(s.data_dir / maintenance.DB_NAME)
        auth = AuthService(db, s)
        if a.cmd == "migrate":
            print("applied:", db.migrations_applied or "none (already current)")
        elif a.cmd == "verify-audit":
            r = db.verify_audit()
            print(json.dumps(r, indent=1))
            return 0 if r["ok"] else 1
        elif a.cmd == "list-users":
            for u in auth.users():
                print(f"{u['name']:20} {u['role']:6} {'DISABLED' if u['disabled'] else 'active':9} expires={u['expires_at'] or '-'} last_used={u['last_used'] or '-'}")
        elif a.cmd == "create-user":
            u, token = auth.create_user(a.name, a.role, "cli", a.ttl_days)
            print(f"user {u['name']} ({u['role']}) created. Token (shown once): {token}")
        elif a.cmd == "rotate":
            u, token = auth.rotate(a.name, "cli"); print(f"new token for {u['name']} (shown once): {token}")
        elif a.cmd in ("disable", "enable"):
            auth.set_disabled(a.name, a.cmd == "disable", "cli"); print(f"{a.name}: {a.cmd}d")
        elif a.cmd == "grant":
            print(auth.set_member(a.project, a.user, a.role, "cli"))
    except (Invalid, Conflict, NotFound) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
