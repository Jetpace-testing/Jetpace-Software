"""
Subscribe to the broker and write what arrives into the historian.

The other half of MqttSink. Between them the poller no longer touches the
database at all, which is what makes the acquisition tier replaceable -
and what lets a second consumer (the rule engine, next) read the same
stream without anybody polling anything.

Note what it does NOT do: it does not re-implement writing. It decodes
messages into the same Reading objects the poller produces and hands them
to the same PostgresSink, so there is exactly one piece of code that knows
how a reading becomes a row.

Moved here from scripts/run_ingest.py when the application was frozen:
PyInstaller can only freeze what is importable, and scripts/ is composition
rather than library code.
"""

from __future__ import annotations

import queue
import threading
import time

from .mqtt import QOS, Site, decode_reading, decode_status, parse
from .poller import Reading
from .sinks import PostgresSink

# Readings are batched before being written. One INSERT per message would
# work and would be slower for no benefit; at 400 devices this is the
# difference between a hundred round trips a second and two.
BATCH_SIZE = 500
FLUSH_SECONDS = 2.0
QUEUE_MAX = 50_000

# How long to wait before asking the sink to re-resolve device codes after
# a reading arrives for one it does not recognise.
#
# PostgresSink resolves codes to ids once, at construction. The poller
# calls `refresh()` when it reloads its device list, because it knows when
# that happened; this process has no idea - a device commissioned from the
# dashboard is just a code it has never seen on a topic it is already
# subscribed to. So it reacts to the evidence instead: readings being
# skipped is exactly the symptom, and the symptom is also the signal.
#
# Rate limited because the other reason for an unknown code is a device
# that is genuinely not in this database - a second site publishing to a
# shared broker, a typo in a topic - and that one never resolves. Without
# the floor, a steady trickle of those would re-run two queries per batch
# forever.
UNKNOWN_RECHECK_SECONDS = 30.0


class Ingest:
    def __init__(self, site: Site, sink: PostgresSink) -> None:
        self.site = site
        self.sink = sink
        self.queue: queue.Queue[Reading] = queue.Queue(maxsize=QUEUE_MAX)
        self.stopping = threading.Event()
        self.received = 0
        self.rejected = 0
        self.status_changes = 0
        # How many times an unknown device code made this process
        # re-resolve its lookups. Steadily rising means something is
        # publishing codes this database does not have; one or two means a
        # device was commissioned.
        self.refreshes = 0

    # --- broker callbacks (these run on paho's network thread) ---------

    def on_connect(self, client, userdata, flags, reason_code,
                   properties=None) -> None:
        if reason_code != 0:
            print(f"  connect refused: {reason_code}", flush=True)
            return
        client.subscribe([(self.site.all_telemetry, QOS),
                          (self.site.all_status, QOS)])
        print(f"  subscribed to {self.site.all_telemetry}", flush=True)
        print(f"               {self.site.all_status}", flush=True)

    def on_message(self, client, userdata, msg) -> None:
        topic = parse(msg.topic)
        if topic is None:
            return                  # not ours; a shared broker is normal

        if topic.kind == "status":
            try:
                online, detail, _ = decode_status(msg.payload)
            except Exception:                               # noqa: BLE001
                self.rejected += 1
                return
            self.status_changes += 1
            print(f"  {'up  ' if online else 'DOWN'} {topic.device}  "
                  f"{detail}", flush=True)
            return

        if topic.kind != "telemetry":
            return

        try:
            ts, value, quality = decode_reading(msg.payload)
        except ValueError as exc:
            # A malformed message is dropped and counted, never guessed at.
            self.rejected += 1
            if self.rejected <= 5:
                print(f"  rejected {msg.topic}: {exc}", flush=True)
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
        skipped_seen = self.sink.skipped
        last_refresh = time.monotonic()
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

                # Something arrived for a device this process cannot
                # resolve. Most likely it was commissioned from the
                # dashboard a moment ago and the cache below predates it.
                if (self.sink.skipped > skipped_seen
                        and last - last_refresh >= UNKNOWN_RECHECK_SECONDS):
                    self.sink.refresh()
                    self.refreshes += 1
                    last_refresh = last
                skipped_seen = self.sink.skipped
        if batch:
            self.sink.write(batch)
