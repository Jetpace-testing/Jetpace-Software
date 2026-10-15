"""
Where readings go.

Two implementations of the same Protocol. The poller is constructed with
one of them and cannot tell the difference, which is the seam that lets
transport change without touching acquisition.

  ConsoleSink   prints. For development, and for proving the Modbus half
                works before a database exists.
  PostgresSink  writes to the reading hypertable, idempotently.

A third, MqttSink, is stage 2b. It will implement the same two methods and
nothing above it will change.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from .poller import Reading

log = logging.getLogger(__name__)


class ConsoleSink:
    """Prints one line per device per cycle, and state changes as they happen."""

    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self.count = 0
        self._lock = threading.Lock()

    def write(self, readings: list[Reading]) -> None:
        with self._lock:
            self.count += len(readings)
            if self.quiet:
                return
            by_device: dict[str, list[Reading]] = {}
            for r in readings:
                by_device.setdefault(r.device_code, []).append(r)
            stamp = datetime.now().strftime("%H:%M:%S")
            for code, group in sorted(by_device.items()):
                values = "  ".join(f"{r.param}={r.value:g}" for r in group)
                print(f"  {stamp}  {code:<12} {values}")

    def device_state(self, device_code: str, online: bool, detail: str = "") -> None:
        mark = "up  " if online else "DOWN"
        print(f"  ---- {mark} {device_code}  {detail}")

    def refresh(self) -> None:
        """Nothing cached: this sink resolves nothing and stores nothing."""


class PostgresSink:
    """
    Writes readings to the hypertable.

    Device codes are resolved to ids once and cached, because the poller
    speaks in codes - which are stable forever - while the database stores
    readings against the numeric id.

    The insert is idempotent. Store-and-forward replay delivers at least
    once, so ON CONFLICT DO NOTHING is not an optimisation; without it a
    reconnection would duplicate a day of history.
    """

    INSERT = """
        INSERT INTO reading (ts, device_id, param, value, quality)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (device_id, param, ts) DO NOTHING
    """

    def __init__(self, dsn: str) -> None:
        import psycopg

        self._psycopg = psycopg
        self.dsn = dsn
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._device_ids: dict[str, int] = {}
        self._param_ids: dict[str, int] = {}
        self._load_lookups()
        self.written = 0
        self.skipped = 0

    def _load_lookups(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT code, id FROM device WHERE retired_at IS NULL")
            self._device_ids = {code: did for code, did in cur.fetchall()}
            cur.execute("SELECT key, id FROM parameter")
            self._param_ids = {key: pid for key, pid in cur.fetchall()}
        log.info("resolved %d devices, %d parameters",
                 len(self._device_ids), len(self._param_ids))

    def write(self, readings: list[Reading]) -> None:
        rows = []
        for r in readings:
            device_id = self._device_ids.get(r.device_code)
            param_id = self._param_ids.get(r.param)
            if device_id is None or param_id is None:
                # A reading for something not in the database is dropped and
                # counted, never invented. Commissioning a device is a
                # deliberate act, not a side effect of it answering.
                self.skipped += 1
                continue
            rows.append((r.ts, device_id, param_id, r.value, r.quality))

        if not rows:
            return
        with self._lock:
            with self._conn.cursor() as cur:
                cur.executemany(self.INSERT, rows)
            self.written += len(rows)

    def device_state(self, device_code: str, online: bool, detail: str = "") -> None:
        # Stage 2b publishes this to a retained MQTT status topic, which is
        # what makes "offline" a fact rather than an inference. For now it
        # is only logged.
        log.info("device %s %s %s", device_code,
                 "online" if online else "OFFLINE", detail)

    def refresh(self) -> dict[str, tuple[int, int]]:
        """
        Re-resolve device codes and parameter keys.

        Called by the poller after the device list changes, and this is
        the sink the method exists for. The cache below is loaded once at
        construction; a device commissioned from the dashboard while this
        process is running is absent from it, so `write` would count
        every one of its readings as `skipped` - a device read perfectly
        off the bus, stored nowhere, with nothing in the logs but a
        rising number.

        Under the write lock, because the two queries go down the same
        connection `write` is using and a psycopg connection is not safe
        for concurrent use. Replacing the dicts wholesale rather than
        updating them in place, so a reader between the two statements
        sees one complete mapping or the other.
        """
        with self._lock:
            before = dict(self._device_ids)
            self._load_lookups()

        # A code whose ID CHANGED is not a routine cache update. It means
        # the device row was deleted and recreated - which the
        # application cannot do, because a retired code is refused, so it
        # means somebody used a superuser connection. Every reading
        # written since that happened went to the old id, and `reading`
        # has no foreign key to stop it (0001: it is a hypertable), so
        # they are sitting in the historian attached to nothing.
        #
        # WARNING and not INFO. This is the only notice anybody gets.
        moved = {code: (old, self._device_ids[code])
                 for code, old in before.items()
                 if code in self._device_ids and self._device_ids[code] != old}
        if moved:
            log.warning(
                "device id changed under us: %s. Readings written since the "
                "change are orphaned - they reference a device row that no "
                "longer exists and will not appear anywhere.",
                ", ".join(f"{c} {o}->{n}" for c, (o, n) in moved.items()))
        return moved

    def close(self) -> None:
        self._conn.close()


class MqttSink:
    """
    Publishes readings to a broker instead of writing them itself.

    Implements exactly the same two methods as ConsoleSink and
    PostgresSink, so the poller cannot tell which it has. That was the
    claim the architecture made about the sink interface, and this class
    is the test of it: nothing above this line in the codebase changed to
    accommodate it.

    Three things it does that a direct writer cannot:

      A WILL. Registered with the broker before connecting, so if this
      process dies - killed, power cut, blue screen - the broker publishes
      "agent offline" on our behalf within the keepalive interval. That is
      the difference between offline being asserted and inferred.

      RETAINED STATUS. The broker keeps the last status for each device,
      so a dashboard connecting at three in the afternoon learns
      immediately which devices are down, without waiting for the next
      change.

      BUFFERING ON DISCONNECT. paho queues QoS 1 messages while offline
      and delivers them on reconnect. That covers a brief outage; the
      poller's own SQLite ring buffer is what covers a long one.

    Telemetry is NOT retained, deliberately. A retained pressure reading
    from three days ago served to a dashboard as current is a hazard.
    """

    def __init__(self, site, host: str = "127.0.0.1", port: int = 1887,
                 client_id: str | None = None,
                 username: str | None = None, password: str | None = None,
                 keepalive: int = 15) -> None:
        # keepalive sets how fast "the agent died" becomes known. A broker
        # declares a client dead after 1.5x keepalive of silence, so 15 s
        # means the will publishes within about 22 s. That IS the product
        # guarantee - not "instantly" - and it is worth stating plainly
        # because a reader will otherwise assume a will is immediate.
        #
        # It is immediate only when the socket closes cleanly. A process
        # force-killed on Windows can leave a HALF-OPEN connection: the
        # process is gone, no FIN is ever sent, and the broker goes on
        # believing the client is there until the keepalive timer fires.
        # Measured here: a clean socket close published the will in under
        # 3 s; a force-killed process did not trigger it at all until the
        # timer caught up.
        import os

        import paho.mqtt.client as paho

        from .mqtt import QOS, encode_reading, encode_status

        # The client ID must be UNIQUE PER PROCESS. MQTT requires a broker
        # to evict any existing session using the same ID, so two agents
        # sharing one do not coexist - they take the session from each
        # other several times a second, forever, and every publish in
        # between fails. Observed here as 28 "session taken over" lines in
        # a 25 second run.
        #
        # That is not a theoretical concern for this product: the updater
        # restarts the service, and if a predecessor has not fully exited
        # the replacement would fight it rather than start cleanly.
        client_id = client_id or f"jetpace-poller-{os.getpid()}"

        self._paho = paho
        self._encode_reading = encode_reading
        self._encode_status = encode_status
        self._qos = QOS
        self.site = site
        self.published = 0
        self.queued = 0
        self.failed = 0

        self._client = paho.Client(
            paho.CallbackAPIVersion.VERSION2, client_id=client_id,
            protocol=paho.MQTTv5, clean_session=None)
        if username:
            self._client.username_pw_set(username, password)

        # The will must be set BEFORE connect() - it is sent as part of the
        # CONNECT packet, so setting it afterwards does nothing at all.
        self._client.will_set(
            site.agent_status,
            encode_status(False, "agent stopped without saying goodbye"),
            qos=QOS, retain=True)

        self._client.connect(host, port, keepalive=keepalive)
        self._client.loop_start()
        self._client.publish(site.agent_status,
                             encode_status(True, "agent started"),
                             qos=QOS, retain=True)
        log.info("mqtt sink connected to %s:%s as %s", host, port, client_id)

    def write(self, readings: list[Reading]) -> None:
        for r in readings:
            topic = self.site.telemetry(r.device_code, r.param)
            info = self._client.publish(
                topic, self._encode_reading(r.ts, r.value, r.quality),
                qos=self._qos, retain=False)
            if info.rc == self._paho.MQTT_ERR_SUCCESS:
                self.published += 1
            elif info.rc == self._paho.MQTT_ERR_NO_CONN:
                # NOT a failure. paho holds QoS 1 messages while the
                # connection is down and delivers them on reconnect, so
                # counting these as failed reports a problem that is not
                # happening - which is worse than silence, because an
                # operator acts on it.
                self.queued += 1
            else:
                # Never raised: the poller's job is to keep polling. The
                # local buffer is what makes a real loss recoverable.
                self.failed += 1
                log.warning("publish to %s failed: rc=%s", topic, info.rc)

    def device_state(self, device_code: str, online: bool,
                     detail: str = "") -> None:
        self._client.publish(
            self.site.status(device_code),
            self._encode_status(online, detail),
            qos=self._qos, retain=True)

    def refresh(self) -> None:
        """
        Nothing cached. This sink publishes a device CODE on a topic and
        never resolves it to anything, which is why the ingest process on
        the other end is where a newly commissioned device has to be
        recognised.
        """

    def close(self) -> None:
        """Say goodbye properly, so the will is not what the broker sends."""
        self._client.publish(self.site.agent_status,
                             self._encode_status(False, "agent stopped"),
                             qos=self._qos, retain=True)
        self._client.loop_stop()
        self._client.disconnect()
