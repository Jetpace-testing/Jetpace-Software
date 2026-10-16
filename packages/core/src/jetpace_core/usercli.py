"""
Accounts, from the machine itself.

THE WAY BACK IN. Everything else about users happens in the browser, and
this exists for the two occasions the browser cannot help: nobody can
sign in, and the only administrator has left the company. It needs the
database password, which means it needs the machine - which is the right
bar for a command that can mint an administrator.

A separate module from cli.py because that file is argument parsing for
three long-running processes and this is five short commands about
people, and because the one thing worth reading carefully here -
`_ask_password` - should not be buried in the middle of a poller.
"""

from __future__ import annotations

import argparse
import getpass

from . import paths
from .security import PasswordRejected
from .web import identity, queries
from .web.writes import WriteRejected


def ask_password(prompt: str = "Password") -> str:
    """
    Read a password without echoing it, twice.

    NOT A COMMAND-LINE ARGUMENT, deliberately. A password passed as an
    argument is in the process table for as long as the command runs, in
    the shell history afterwards, and in any terminal recording - which
    on a plant PC being commissioned by somebody with a laptop and a
    screen-sharing session is three places too many.

    `getpass` falls back to echoing with a warning when it has no
    terminal. That is worth refusing rather than accepting, because the
    caller cannot see the warning in a pipeline.
    """
    try:
        first = getpass.getpass(f"{prompt}: ")
        again = getpass.getpass("Again: ")
    except (getpass.GetPassWarning, EOFError):
        raise SystemExit(
            "  This needs a terminal that can hide what you type. Run it "
            "directly rather than through a pipe.") from None
    if first != again:
        raise SystemExit("  The two passwords were not the same.")
    return first


def add(args: argparse.Namespace) -> int:
    roles = tuple(args.role) or ("administrator",)
    password = ask_password()
    try:
        with queries.connect(paths.postgres_dsn()) as conn:
            user = identity.create_user(
                conn, username=args.username, full_name=args.name,
                password=password, roles=roles,
                must_change=args.must_change,
                # NULL, not "cli". §10 wants a person, and a command run
                # on the machine does not identify one. The audit row
                # records that the account was created; it does not
                # invent a name to attribute it to.
                actor=None)
    except (WriteRejected, PasswordRejected) as refused:
        print(f"  refused: {refused}")
        return 1
    print()
    print(f"  created {user['username']} ({args.name}) as "
          f"{', '.join(roles)}")
    if args.must_change:
        print("  they must choose a new password at first sign-in")
    print()
    return 0


def listing(args: argparse.Namespace) -> int:
    with queries.connect(paths.postgres_dsn()) as conn:
        rows = identity.list_users(conn)
    if not rows:
        print()
        print("  No accounts yet. The dashboard asks for the first one,")
        print("  or create it here with `jetpace user add <name>`.")
        print()
        return 0
    print()
    print(f"  {'username':<16}{'name':<22}{'roles':<30}"
          f"{'last sign-in':<16}{'sessions':>9}  state")
    for r in rows:
        last = (r["last_login_at"].strftime("%d %b %H:%M")
                if r["last_login_at"] else "never")
        if r["disabled_at"]:
            state = "disabled"
        elif r["locked_until"]:
            state = "locked"
        elif r["must_change_password"]:
            state = "must change password"
        else:
            state = "active"
        print(f"  {r['username'][:15]:<16}{r['full_name'][:21]:<22}"
              f"{','.join(r['roles'])[:29]:<30}{last:<16}"
              f"{r['live_sessions']:>9}  {state}")
    print()
    return 0


def password(args: argparse.Namespace) -> int:
    new = ask_password("New password")
    try:
        with queries.connect(paths.postgres_dsn()) as conn:
            user = identity.user_by_name(conn, args.username)
            if user is None:
                raise SystemExit(f"  No account called {args.username!r}.")
            identity.set_password(
                conn, user_id=user["id"], username=user["username"],
                full_name=user["full_name"], password=new,
                actor=None, must_change=args.must_change)
    except (WriteRejected, PasswordRejected) as refused:
        print(f"  refused: {refused}")
        return 1
    print()
    print(f"  Set. Every session {args.username} had is ended.")
    if args.must_change:
        print("  They must choose their own at the next sign-in, because")
        print("  an administrator who knows a working password for")
        print("  somebody else's account breaks attribution.")
    print()
    return 0


def _switch(args: argparse.Namespace, off: bool, reason: str = "") -> int:
    with queries.connect(paths.postgres_dsn()) as conn:
        user = identity.user_by_name(conn, args.username)
        if user is None:
            raise SystemExit(f"  No account called {args.username!r}.")
        identity.set_disabled(
            conn, user_id=user["id"], username=user["username"],
            disabled=off, reason=reason, actor=None)
    print()
    print(f"  {args.username} is now "
          f"{'disabled' if off else 'enabled'}.")
    if off:
        print("  The account is switched off, not deleted: an audit row "
              "five years")
        print("  old names its actor, and that has to stay answerable.")
    print()
    return 0


def disable(args: argparse.Namespace) -> int:
    return _switch(args, True, args.reason)


def enable(args: argparse.Namespace) -> int:
    return _switch(args, False)


def attach(subparsers) -> None:
    """Hang `jetpace user ...` off the main parser."""
    p = subparsers.add_parser("user", help="accounts, from the machine itself")
    commands = p.add_subparsers(dest="user_command")

    a = commands.add_parser("add", help="create an account")
    a.add_argument("username")
    a.add_argument("--name", required=True, help="the person's full name")
    a.add_argument("--role", action="append", default=[],
                   help="repeatable; administrator if none is given")
    a.add_argument("--must-change", action="store_true",
                   help="require a new password at first sign-in")
    a.set_defaults(func=add)

    b = commands.add_parser("list", help="who has an account")
    b.set_defaults(func=listing)

    c = commands.add_parser("password", help="set somebody's password")
    c.add_argument("username")
    c.add_argument("--keep", dest="must_change", action="store_false",
                   default=True,
                   help="do NOT require them to change it; only for your "
                        "own account")
    c.set_defaults(func=password)

    d = commands.add_parser("disable", help="switch an account off")
    d.add_argument("username")
    d.add_argument("--reason", default="")
    d.set_defaults(func=disable)

    e = commands.add_parser("enable", help="switch one back on")
    e.add_argument("username")
    e.set_defaults(func=enable)

    def help_only(_args):
        p.print_help()
        return 2

    p.set_defaults(func=help_only)
