"""
Stand up a private PostgreSQL + TimescaleDB for development.

This is deliberately the same sequence the installer will run on a customer
PC, in the same order, with the same decisions - a private copy of Postgres
on a non-default port, its own data directory, loopback only, scram
authentication, a generated password. Getting it right here means the
hardest part of the installer is already proven.

What it does NOT do, because a dev machine does not need it: register
Windows services, create a service account, or set directory ACLs. That
part comes with the installer, and it is the remaining risk.

    python scripts/setup_postgres.py --pg-zip ~/Downloads/postgresql-17.x-windows-x64-binaries.zip
    python scripts/setup_postgres.py --start
    python scripts/setup_postgres.py --status
    python scripts/setup_postgres.py --stop

Re-running setup is safe: an initialised data directory is left alone.

PostgreSQL's zip archive has no stable published URL - EDB hosts it behind
a landing page at https://www.enterprisedb.com/download-postgresql-binaries
- so it is downloaded by hand once and pinned here by hash. TimescaleDB's
release assets do have predictable URLs, so that one is fetched for you.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import re
import urllib.request
import zipfile
from pathlib import Path

# The pinned pair. The TimescaleDB extension is compiled against ONE
# PostgreSQL major version; a build for 16 dropped beside a 17 tree gives a
# database that will not start at all.
#
# Rather than trust this constant, the major version is read back out of
# the extracted binaries and the matching TimescaleDB asset is chosen from
# that. The mismatch cannot happen.
PG_MAJOR = 17
TIMESCALE_VERSION = "2.30.2"

# Which PostgreSQL majors this TimescaleDB release publishes Windows builds
# for. Checked against the release assets, not guessed.
TIMESCALE_SUPPORTS = (16, 17, 18)

# EDB hosts the binaries zip behind a landing page and the fileid is
# specific to each point release, so this WILL go stale. When it does, get
# the current link from
# https://www.enterprisedb.com/download-postgresql-binaries
# and pass it with --pg-url, or download by hand and use --pg-zip.
PG_ZIP_URLS = {
    17: "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260616",  # 17.11
    18: "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260609",  # 18.6
}


def timescale_url(major: int) -> str:
    return (f"https://github.com/timescale/timescaledb/releases/download/"
            f"{TIMESCALE_VERSION}/"
            f"timescaledb-postgresql-{major}-windows-amd64.zip")

PORT = 5433                  # never 5432: a customer may already have one
SUPERUSER = "jetpace_super"
APP_ROLE = "jetpace_app"
APP_DB = "jetpace"

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "installer" / "vendor"
PGSQL = VENDOR / "pgsql"
LOCK = VENDOR / "vendor.lock.json"

LOCAL = ROOT / ".local"      # gitignored: dev data, logs, secrets
DATA = LOCAL / "pgdata"
LOGS = LOCAL / "logs"
SECRET = LOCAL / "pg_password"

# Directories inside the Postgres zip that are useless on a site PC.
# Roughly halves 350 MB. Test initdb after any further trimming - it fails
# in confusing ways when a template file is missing.
TRIM = ("include", "doc", "symbols", "pgAdmin 4")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"\nERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_lock() -> dict:
    return json.loads(LOCK.read_text()) if LOCK.exists() else {}


def write_lock(entries: dict) -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps(entries, indent=2) + "\n")


def bin_path(name: str) -> Path:
    exe = PGSQL / "bin" / f"{name}.exe"
    if not exe.exists():
        die(f"{exe} is missing. Run setup with --pg-zip first.")
    return exe


def run(args: list[str], timeout: float = 120.0, **kw) -> subprocess.CompletedProcess:
    """
    Every external command gets a timeout. Without one, a single blocked
    child turns the whole script into a silent hang with no indication of
    which step it died on - which is exactly how the first run of this
    script failed.
    """
    argv = [str(a) for a in args]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, **kw)
    except subprocess.TimeoutExpired as exc:
        die(f"{Path(argv[0]).name} did not finish within {timeout:.0f}s\n"
            f"  command: {' '.join(argv)}\n"
            f"  stdout:  {(exc.stdout or b'')[:400]!r}\n"
            f"  stderr:  {(exc.stderr or b'')[:400]!r}")


# ---------------------------------------------------------------------------
# vendor payloads
# ---------------------------------------------------------------------------

def fetch_postgres(url: str) -> Path:
    """Download the binaries zip to .local/ so it is cached between runs."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    dest = LOCAL / "postgresql-binaries.zip"
    if dest.exists() and dest.stat().st_size > 100_000_000:
        say(f"using cached {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)")
        return dest

    say(f"downloading PostgreSQL binaries - this is about 350 MB")
    seen = [0]

    def progress(block: int, size: int, total: int) -> None:
        done = block * size
        if total > 0 and done - seen[0] > 25_000_000:
            seen[0] = done
            print(f"    {done / 1e6:>5.0f} / {total / 1e6:.0f} MB", flush=True)

    try:
        urllib.request.urlretrieve(url, dest, reporthook=progress)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        die(
            f"download failed: {exc}\n  URL: {url}\n\n"
            "EDB's fileid changes with every point release. Get the current "
            "Windows x86-64 link from\n"
            "  https://www.enterprisedb.com/download-postgresql-binaries\n"
            "and pass it with --pg-url, or download by hand and use --pg-zip."
        )

    if dest.stat().st_size < 100_000_000:
        size = dest.stat().st_size
        dest.unlink()
        die(
            f"the download is only {size / 1e6:.1f} MB, so it is not the "
            "binaries zip - most likely an HTML error page. Check the URL."
        )
    say(f"downloaded {dest.stat().st_size / 1e6:.0f} MB")
    return dest


def extract_postgres(zip_path: Path) -> None:
    if not zip_path.exists():
        die(f"{zip_path} does not exist")
    if zip_path.suffix.lower() == ".exe":
        die(
            f"{zip_path.name} is the interactive installer, not the binaries "
            "zip.\n"
            "The installer registers its own system-wide service on port 5432 "
            "and writes to the registry, which is exactly what a bundled "
            "product must not do.\n"
            "Get the 'zip archive of the binaries' instead."
        )

    digest = sha256(zip_path)
    lock = read_lock()
    pinned = lock.get("postgresql", {}).get("sha256")

    if pinned and pinned != digest:
        die(
            f"{zip_path.name} does not match the pinned hash.\n"
            f"  pinned {pinned}\n  actual {digest}\n"
            "Either this is a different file, or the download is corrupt."
        )

    if PGSQL.exists():
        say(f"pgsql/ already extracted, leaving it alone")
    else:
        say(f"extracting {zip_path.name} ({zip_path.stat().st_size / 1e6:.0f} MB)")
        VENDOR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(VENDOR)
        if not PGSQL.exists():
            die(f"the zip did not contain a pgsql/ directory")

        for d in TRIM:
            victim = PGSQL / d
            if victim.exists():
                size = sum(f.stat().st_size for f in victim.rglob("*") if f.is_file())
                shutil.rmtree(victim)
                say(f"trimmed {d}/ ({size / 1e6:.0f} MB)")

    lock.setdefault("postgresql", {}).update(
        {"file": zip_path.name, "sha256": digest, "major": PG_MAJOR}
    )
    write_lock(lock)
    say(f"PostgreSQL pinned: sha256 {digest[:16]}...")


def detect_pg_major() -> int:
    """
    Read the major version out of the binaries actually on disk, so the
    TimescaleDB build is chosen to match them rather than to match a
    constant somebody forgot to update.
    """
    r = run([bin_path("postgres"), "--version"])
    text = (r.stdout + r.stderr).strip()
    m = re.search(r"\b(\d+)\.\d+", text)
    if not m:
        die(f"could not read a version from: {text!r}")
    major = int(m.group(1))
    say(f"extracted PostgreSQL is {text.split()[-1]} (major {major})")
    return major


def install_timescale(major: int) -> None:
    marker = PGSQL / "share" / "extension" / "timescaledb.control"
    if marker.exists():
        say("TimescaleDB already in place")
        return

    if major not in TIMESCALE_SUPPORTS:
        die(
            f"TimescaleDB {TIMESCALE_VERSION} publishes no Windows build for "
            f"PostgreSQL {major}.\n"
            f"  supported: {', '.join(map(str, TIMESCALE_SUPPORTS))}\n"
            "Use one of those majors, or a TimescaleDB release that covers this one."
        )

    TIMESCALE_URL = timescale_url(major)
    say(f"downloading TimescaleDB {TIMESCALE_VERSION} for PostgreSQL {major}")
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "timescale.zip"
        try:
            urllib.request.urlretrieve(TIMESCALE_URL, zip_path)
        except Exception as exc:
            die(f"could not download TimescaleDB: {exc}\n  URL: {TIMESCALE_URL}")

        digest = sha256(zip_path)
        unpacked = Path(tmp) / "unpacked"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(unpacked)

        # Do NOT run the setup.exe inside: it expects an already-installed
        # PostgreSQL with pg_config on the PATH, which is exactly the
        # interactive behaviour the bundled approach exists to avoid.
        # Place the files by pattern instead, and discover them rather than
        # assuming the zip's internal layout.
        placed = 0
        for f in unpacked.rglob("*"):
            if not f.is_file() or not f.name.startswith("timescaledb"):
                continue
            if f.suffix == ".dll":
                dest = PGSQL / "lib" / f.name
            elif f.suffix in (".control", ".sql"):
                dest = PGSQL / "share" / "extension" / f.name
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            placed += 1

        if not marker.exists():
            die(
                f"placed {placed} files but timescaledb.control is missing.\n"
                "The zip's layout may have changed; inspect it by hand."
            )
        say(f"placed {placed} TimescaleDB files")

    lock = read_lock()
    lock.setdefault("timescaledb", {}).update(
        {"version": TIMESCALE_VERSION, "sha256": digest, "pg_major": major}
    )
    write_lock(lock)


# ---------------------------------------------------------------------------
# the cluster
# ---------------------------------------------------------------------------

CONF = f"""
# written by scripts/setup_postgres.py - edit the script, not this file

listen_addresses = '127.0.0.1'        # never the network
port = {PORT}                         # never the default

shared_preload_libraries = 'timescaledb'

shared_buffers = 512MB                # the installer sets this from real RAM
work_mem = 32MB
maintenance_work_mem = 256MB
effective_cache_size = 2GB

wal_compression = on
checkpoint_completion_target = 0.9

logging_collector = on
log_directory = 'log'
log_rotation_age = 1d
log_min_duration_statement = 2000     # anything slow enough to notice

timezone = 'Asia/Kolkata'             # storage stays UTC; this is display
"""

HBA = """# loopback only, scram only. Nothing else, ever.
host  all  all  127.0.0.1/32  scram-sha-256
host  all  all  ::1/128       scram-sha-256
"""


def initdb() -> None:
    if (DATA / "PG_VERSION").exists():
        say("data directory already initialised, leaving it alone")
        return

    # The check that stands between a reinstall and an erased year of
    # billing history. A non-empty directory without PG_VERSION is not
    # something to run initdb over.
    if DATA.exists() and any(DATA.iterdir()):
        die(
            f"{DATA} is not empty but has no PG_VERSION.\n"
            "Refusing to initdb over it. Move it aside by hand if you are sure."
        )

    password = secrets.token_urlsafe(24)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".pw") as pw:
        pw.write(password)
        pwfile = Path(pw.name)

    try:
        say("running initdb")
        # -E UTF8 --locale=C avoids the Windows locale naming mess entirely,
        # and is the right choice anyway: collation that varies with the
        # machine's regional settings is a bug waiting for a report to come
        # out in a different order at a different site.
        r = run([
            bin_path("initdb"),
            "-D", DATA,
            "-E", "UTF8",
            "--locale=C",
            "-U", SUPERUSER,
            "--auth-host=scram-sha-256",
            f"--pwfile={pwfile}",
        ])
        if r.returncode != 0:
            die(f"initdb failed:\n{r.stdout}\n{r.stderr}")
    finally:
        pwfile.unlink(missing_ok=True)

    (DATA / "postgresql.auto.conf").write_text(CONF.lstrip())
    (DATA / "pg_hba.conf").write_text(HBA)

    SECRET.write_text(password)
    if os.name == "nt":
        run(["icacls", str(SECRET), "/inheritance:r", "/grant:r",
             f"{os.environ.get('USERNAME', '')}:F"])
    say(f"superuser password written to {SECRET.relative_to(ROOT)}")


def is_running() -> bool:
    """pg_ctl status exits 0 when the server is up, 3 when it is not."""
    return pg_ctl("status").returncode == 0


def ensure_started() -> None:
    """
    Start the cluster unless it is already up.

    Checking first rather than parsing pg_ctl's output: it says "another
    server might be running", not "already running", and on Windows it also
    fails outright if anything else holds the log file open. Asking the
    question is more reliable than matching the error.
    """
    if is_running():
        say(f"already running on 127.0.0.1:{PORT}")
        return
    r = pg_ctl("start")
    if r.returncode != 0:
        die(f"pg_ctl start exited {r.returncode}; see "
            f"{LOGS / 'postgres.log'} and {DATA / 'log'}")
    if not wait_ready():
        die(f"started but not answering on 127.0.0.1:{PORT}; see {DATA / 'log'}")
    say(f"listening on 127.0.0.1:{PORT}")


def start_detached(args: list[str]) -> subprocess.CompletedProcess:
    """
    Start the postmaster without inheriting our pipes, and outside our
    process group. Two separate Windows problems, both of which bit here:

      Capturing output HANGS. pg_ctl itself returns quickly, but the
      postmaster it spawned inherits the stdout and stderr pipes and holds
      them open for as long as the database runs - so subprocess.run waits
      for an EOF that never comes. `pg_ctl -l` already writes the server
      log to a file, so there is nothing worth capturing anyway.

      Without DETACHED_PROCESS the postmaster stays inside this process
      tree, so anything that kills the script takes the database down with
      it. That is precisely what happened: killing a hung script shut the
      cluster down mid-transaction, and the log recorded a server process
      "terminated by exception 0xC0000142" followed by the whole cluster
      shutting itself down.

    The real deployment sidesteps both by registering Postgres as a Windows
    SERVICE, owned by the service manager rather than by whatever shell
    happened to start it. This is the development stand-in for that, and
    the reason the installer must not skip the service registration.
    """
    argv = [str(a) for a in args]

    # Flag sets to try, most-detached first. CREATE_BREAKAWAY_FROM_JOB is
    # the one that matters when the launching shell is inside a Windows JOB
    # OBJECT - a CI runner, a task wrapper, an IDE terminal. A job kills
    # everything in it when it ends, and DETACHED_PROCESS does not escape
    # that; without breakaway the database dies the moment the shell that
    # started it exits, which looked exactly like a crash. Not every job
    # permits breakaway, so fall back rather than fail.
    attempts: list[int] = [0]
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        attempts = [base | subprocess.CREATE_BREAKAWAY_FROM_JOB, base]

    last: Exception | None = None
    for flags in attempts:
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                creationflags=flags,
            )
            return subprocess.CompletedProcess(argv, proc.returncode, "", "")
        except subprocess.TimeoutExpired:
            die(f"pg_ctl start did not return within 90s; "
                f"see {LOGS / 'postgres.log'}")
        except OSError as exc:
            last = exc            # job forbids breakaway; try the next set
    die(f"could not start pg_ctl: {last}")


def pg_ctl(action: str) -> subprocess.CompletedProcess:
    args = [bin_path("pg_ctl"), "-D", DATA]
    if action == "start":
        args += ["-l", LOGS / "postgres.log", "-w", "-t", "60", "start"]
        return start_detached(args)
    args += [action]
    return run(args)


def wait_ready(seconds: int = 30) -> bool:
    for _ in range(seconds * 2):
        r = run([bin_path("pg_isready"), "-h", "127.0.0.1", "-p", str(PORT)])
        if r.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def psql(sql: str, database: str = "postgres") -> subprocess.CompletedProcess:
    env = dict(os.environ, PGPASSWORD=SECRET.read_text().strip())
    return run(
        [bin_path("psql"), "-h", "127.0.0.1", "-p", str(PORT),
         "-U", SUPERUSER, "-d", database, "-v", "ON_ERROR_STOP=1",
         "-X", "-q", "-t", "-A", "-c", sql],
        env=env,
    )


def psql_file(path: Path, database: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PGPASSWORD=SECRET.read_text().strip())
    return run(
        [bin_path("psql"), "-h", "127.0.0.1", "-p", str(PORT),
         "-U", SUPERUSER, "-d", database, "-v", "ON_ERROR_STOP=1",
         "-X", "-q", "-f", str(path)],
        env=env,
    )


def create_database() -> None:
    r = psql(f"SELECT count(*) FROM pg_database WHERE datname = '{APP_DB}'")
    if r.returncode != 0:
        die(f"could not query pg_database:\n{r.stdout}\n{r.stderr}")
    if r.stdout.strip().splitlines()[0].strip() == "1":
        say(f"database {APP_DB} already exists")
    else:
        app_password = secrets.token_urlsafe(24)
        for stmt in (
            f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{app_password}'",
            f"CREATE DATABASE {APP_DB} OWNER {APP_ROLE}",
        ):
            r = psql(stmt)
            if r.returncode != 0 and "already exists" not in r.stderr:
                die(f"{stmt.split()[1]} failed:\n{r.stderr}")
        (LOCAL / "app_password").write_text(app_password)
        say(f"created role {APP_ROLE} and database {APP_DB}")

    r = psql("CREATE EXTENSION IF NOT EXISTS timescaledb", database=APP_DB)
    if r.returncode != 0:
        die(f"could not enable TimescaleDB:\n{r.stderr}")
    r = psql("SELECT extversion FROM pg_extension WHERE extname='timescaledb'",
             database=APP_DB)
    say(f"TimescaleDB enabled: {r.stdout.strip() or 'unknown version'}")


GRANTS = f"""
-- The migrations run as the superuser, so every object is owned by it and
-- the application role starts with no rights at all. Grant what the
-- application actually needs and nothing more.
GRANT USAGE ON SCHEMA public TO {APP_ROLE};

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO {APP_ROLE};
GRANT USAGE, SELECT                 ON ALL SEQUENCES  IN SCHEMA public TO {APP_ROLE};
GRANT EXECUTE                       ON ALL FUNCTIONS  IN SCHEMA public TO {APP_ROLE};

-- Readings are INSERT and SELECT only. The application has no business
-- rewriting or deleting a measurement, and under 21 CFR Part 11 neither
-- has anybody else: a correction is a new record with a reason, never an
-- edit in place. Retention and compression run as the owner, not as this
-- role, so ageing data out still works.
REVOKE UPDATE, DELETE ON reading FROM {APP_ROLE};

-- Objects created by future migrations are covered too, so this does not
-- have to be remembered on every schema change.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE};
"""


def grant_privileges() -> None:
    r = psql(GRANTS, database=APP_DB)
    if r.returncode != 0:
        die(f"granting privileges failed:\n{r.stdout}\n{r.stderr}")
    say(f"granted {APP_ROLE}: read/write on config, append-only on reading")


def applied_versions() -> set[int]:
    """
    Which migrations this database already has. Empty on a fresh one, where
    schema_migrations does not exist yet and the query fails - which is the
    correct answer, not an error.
    """
    r = psql("SELECT version FROM schema_migrations ORDER BY version",
             database=APP_DB)
    if r.returncode != 0:
        return set()
    return {int(t) for t in r.stdout.split() if t.strip().isdigit()}


def migrate() -> None:
    """
    Apply migrations that have not been applied, in order.

    The version comes from the filename prefix. Skipping what is already
    recorded is the entire point of having a schema_migrations table - the
    first version of this ran every file on every start, which meant the
    second run of setup failed on "relation already exists".
    """
    files = sorted((ROOT / "migrations").glob("*.sql"))
    if not files:
        say("no migrations found")
        return

    done = applied_versions()
    for f in files:
        m = re.match(r"(\d+)", f.name)
        if not m:
            die(f"{f.name} does not start with a version number")
        version = int(m.group(1))
        if version in done:
            say(f"skipped  {f.name} (already applied)")
            continue
        r = psql_file(f, APP_DB)
        if r.returncode != 0:
            die(f"{f.name} failed:\n{r.stdout}\n{r.stderr}")
        say(f"applied  {f.name}")

    r = psql("SELECT coalesce(max(version), 0) FROM schema_migrations",
             database=APP_DB)
    say(f"schema version: {r.stdout.strip()}")


# ---------------------------------------------------------------------------

def status() -> None:
    r = pg_ctl("status")
    print(r.stdout.strip() or r.stderr.strip())
    if SECRET.exists():
        print(f"\n  connection string for the app:")
        print(f"    postgresql://{APP_ROLE}@127.0.0.1:{PORT}/{APP_DB}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pg-zip", type=Path,
                    help="path to an already-downloaded EDB binaries zip")
    ap.add_argument("--pg-url", help="download the binaries zip from here instead")
    ap.add_argument("--pg-major", type=int, default=PG_MAJOR,
                    help=f"which major to download when neither is given "
                         f"(default {PG_MAJOR})")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        return status()
    if args.stop:
        r = pg_ctl("stop")
        return say(r.stdout.strip() or r.stderr.strip())
    if args.start:
        return ensure_started()

    # full setup
    print("\nJetpace development database\n")
    if args.pg_zip:
        extract_postgres(args.pg_zip)
    elif not PGSQL.exists():
        url = args.pg_url or PG_ZIP_URLS.get(args.pg_major)
        if not url:
            die(
                f"no download URL known for PostgreSQL {args.pg_major}.\n"
                "Get the Windows x86-64 'zip archive of the binaries' from\n"
                "  https://www.enterprisedb.com/download-postgresql-binaries\n"
                "and pass --pg-url, or download it and pass --pg-zip."
            )
        extract_postgres(fetch_postgres(url))
    else:
        say("pgsql/ present")

    major = detect_pg_major()
    install_timescale(major)
    initdb()

    ensure_started()
    create_database()
    migrate()
    grant_privileges()

    print(f"\nReady.\n")
    print(f"  connection   postgresql://{APP_ROLE}@127.0.0.1:{PORT}/{APP_DB}")
    print(f"  password     {(LOCAL / 'app_password').relative_to(ROOT)}")
    print(f"  stop it      python scripts/setup_postgres.py --stop\n")


if __name__ == "__main__":
    main()
