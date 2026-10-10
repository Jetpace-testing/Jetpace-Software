"""
Subscribe to the broker and write what arrives into the historian.

This is the other half of MqttSink. Between them the poller no longer
touches the database at all, which is what makes the acquisition tier
replaceable - and what lets a second consumer (the rule engine, next) read
the same stream without anybody polling anything.

    uv run python scripts/run_ingest.py
    uv run python scripts/run_ingest.py --host 127.0.0.1 --port 1887

Note what it does NOT do: it does not re-implement writing. It decodes
messages into the same Reading objects the poller produces and hands them
to the same PostgresSink, so there is exactly one piece of code that knows
how a reading becomes a row.
"""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "core" / "src"))

import paho.mqtt.client as paho                              # noqa: E402

from jetpace_core.mqtt import (                              # noqa: E402
    QOS, Site, decode_reading, decode_status, parse,
)
from jetpace_core.poller import Reading                      # noqa: E402
from jetpace_core.sinks import PostgresSink                   # noqa: E402

# Readings are batched before being written. One INSERT per message would
# work and would be slower for no benefit; at 400 devices this is the
# difference between a hundred round trips a second and two.
BATCH_SIZE = 500
FLUSH_SECONDS = 2.0


def dsn() -> str:
    pw = ROOT / ".local" / "app_password"
    if not pw.exists():
        raise SystemExit("No database. Run scripts/setup_postgres.py first.")
    return (f"postgresql://jetpace_app:{pw.read_text().strip()}"
            f"@127.0.0.1:5433/jetpace")


class Ingest:
    def __init__(self, site: Site, sink: PostgresSink) -> None:
        self.site = site
        self.sink = sink
        self.queue: queue.Queue[Reading] = queue.Queue(maxsize=50_000)
        self.stopping = threading.Event()
        self.received = 0
        self.rejected = 0
        self.status_changes = 0

    # --- broker callbacks (these run on paho's network thread) ---------

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            print(f"  connect refused: {reason_code}")
            return
        client.subscribe([(self.site.all_telemetry, QOS),
                          (self.site.all_status, QOS)])
        print(f"  subscribed to {self.site.all_telemetry}")
        print(f"               {self.site.all_status}")

    def on_message(self, client, userdata, msg):
        topic = parse(msg.topic)
        if topic is None:
            return                      # not ours; a shared broker is normal

        if topic.kind == "status":
            try:
                online, detail, _ = decode_status(msg.payload)
            except Exception:           # noqa: BLE001
                self.rejected += 1
                return
            self.status_changes += 1
            print(f"  {'up  ' if online else 'DOWN'} {topic.device}  {detail}")
            return

        if topic.kind != "telemetry":
            return

        try:
            ts, value, quality = decode_reading(msg.payload)
        except ValueError as exc:
            # A malformed message is dropped and counted, never guessed at.
            self.rejected += 1
            if self.rejected <= 5:
                print(f"  rejected {msg.topic}: {exc}")
            return

        self.received += 1
        try:
            self.queue.put_nowait(Reading(ts=ts, device_code=topic.device,
                                          param=topic.metric, value=value,
                                          quality=quality))
        except queue.Full:
            # Back pressure: the database is slower than the stream. Drop
            # and count rather than grow without bound - the broker still
            # holds the messages for a session that reconnects, and the
            # poller's own buffer is the real safety net.
            self.rejected += 1

    # --- the writer thread ---------------------------------------------

    def drain(self) -> None:
        batch: list[Reading] = []
        last = time.monotonic()
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                batch.append(self.queue.get(timeout=0.25))
            except queue.Empty:
                pass
            due = (len(batch) >= BATCH_SIZE
                   or (batch and time.monotonic() - last >= FLUSH_SECONDS))
            if due:
                self.sink.write(batch)
                batch.clear()
                last = time.monotonic()
        if batch:
            self.sink.write(batch)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1887)
    ap.add_argument("--tenant", default="jetpace")
    ap.add_argument("--site", default="prototype")
    ap.add_argument("--seconds", type=float, help="stop after this long")
    args = ap.parse_args()

    site = Site(tenant=args.tenant, site=args.site)
    sink = PostgresSink(dsn())
    ingest = Ingest(site, sink)

    client = paho.Client(paho.CallbackAPIVersion.VERSION2,
                         client_id="jetpace-ingest", protocol=paho.MQTTv5)
    pw = ROOT / ".local" / "mqtt_password"
    if pw.exists():
        client.username_pw_set("jetpace_app", pw.read_text().strip())
    client.on_connect = ingest.on_connect
    client.on_message = ingest.on_message

    print(f"\ningest: {args.host}:{args.port} -> postgres")
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()

    writer = threading.Thread(target=ingest.drain, name="ingest-writer",
                              daemon=True)
    writer.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    started = time.monotonic()
    try:
        while not stop.is_set():
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
            stop.wait(0.2)
    finally:
        print("\nstopping")
        ingest.stopping.set()
        writer.join(timeout=10)
        client.loop_stop()
        client.disconnect()
        print(f"  received {ingest.received}  rejected {ingest.rejected}  "
              f"status changes {ingest.status_changes}")
        print(f"  written  {sink.written}   skipped {sink.skipped}")
        sink.close()


if __name__ == "__main__":
    main()
