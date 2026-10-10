"""
A fake Jetpace iDP-100 differential pressure transmitter, over Modbus TCP.

Run this and the poller has something to talk to - no hardware, no
converter, no cleanroom. It serves several slave units on one port, the
way a real serial-to-TCP converter serves several devices on one RS-485
bus.

    python tools/sim/idp100_sim.py                 # 3 devices on port 5020
    python tools/sim/idp100_sim.py --units 8       # 8 devices
    python tools/sim/idp100_sim.py --port 5021

This is written out by hand rather than with a Modbus library, for two
reasons. It removes a dependency from the one component that must always
work, and the wire format is worth seeing once - it is six fields, and
every Modbus bug you will ever chase is in one of them.

=== Modbus TCP, in full ===

Request to read holding registers:

    bytes  field              example   meaning
    0-1    transaction id     00 01     echoed back, so a client can match
                                        a reply to its request
    2-3    protocol id        00 00     always zero for Modbus TCP
    4-5    length             00 06     how many bytes follow this field
    6      unit id            01        WHICH DEVICE on the bus - the slave
                                        address. The whole reason one IP
                                        can serve 32 meters.
    7      function code      03        3 = read holding registers
    8-9    start address      00 00     ZERO for datasheet 40001. Off-by-one
                                        here is the classic Modbus mistake.
    10-11  quantity           00 01     how many registers to read

Reply:

    0-5    same header, length recomputed
    6      unit id            01
    7      function code      03        echoed on success
    8      byte count         02        2 x quantity
    9+     data                         the registers, big-endian, in order

On failure the function code comes back with the top bit set (03 -> 83)
followed by one exception byte. 02 means "that address does not exist".
"""

import argparse
import random
import socket
import socketserver
import struct
import threading
import time

READ_HOLDING_REGISTERS = 3
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_FUNCTION = 0x01

MAX_REGISTERS = 125


# --------------------------------------------------------------------------
# the devices
# --------------------------------------------------------------------------

class Device:
    """One iDP-100. Addresses are wire addresses: datasheet number - 40001."""

    def __init__(self, unit_id: int, room: str) -> None:
        self.unit_id = unit_id
        self.room = room
        self.lock = threading.Lock()
        self.registers: dict[int, int] = {
            13: 1,            # 40014 unit: 1 = Pa
            14: to_u16(-50),  # 40015 lower range
            17: to_u16(50),   # 40018 higher range
            20: unit_id,      # 40021 slave address
            21: 2,            # 40022 baud code: 2 = 38400
        }
        self.set_dp(12.0)

    def set_dp(self, pascals: float) -> None:
        with self.lock:
            self.registers[0] = to_u16(int(round(pascals)))

    def read(self, start: int, count: int) -> list[int]:
        with self.lock:
            return [self.registers.get(start + i, 0) for i in range(count)]


def to_u16(value: int) -> int:
    """Signed value as the unsigned 16 bits that go on the wire. -5 -> 65531."""
    return value & 0xFFFF


# --------------------------------------------------------------------------
# what the pressure does
# --------------------------------------------------------------------------

def simulate(devices: list[Device], stop: threading.Event) -> None:
    """
    A plausible cleanroom cascade: each room sits at a positive setpoint
    with a little noise, and now and then a door is opened and the room
    loses pressure - sometimes going negative, which is the excursion the
    real product exists to catch.
    """
    setpoints = {d.unit_id: 10.0 + 2.5 * i for i, d in enumerate(devices)}
    door_open_until: dict[int, float] = {}

    while not stop.is_set():
        now = time.monotonic()
        for d in devices:
            setpoint = setpoints[d.unit_id]

            if now < door_open_until.get(d.unit_id, 0.0):
                target = -3.0              # door open: cascade reversed
            else:
                target = setpoint
                if random.random() < 0.004:  # roughly once a minute per room
                    door_open_until[d.unit_id] = now + random.uniform(8, 20)
                    print(f"  [sim] unit {d.unit_id} ({d.room}): door opened")

            d.set_dp(target + random.gauss(0, 0.4))
        stop.wait(1.0)


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------

class Handler(socketserver.BaseRequestHandler):

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        print(f"  [sim] connected   {peer}")
        try:
            while True:
                header = recv_exactly(self.request, 7)
                if header is None:
                    break
                txn, proto, length, unit = struct.unpack(">HHHB", header)
                body = recv_exactly(self.request, length - 1)
                if body is None:
                    break
                reply = self.respond(txn, unit, body)
                if reply:
                    self.request.sendall(reply)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            print(f"  [sim] disconnected {peer}")

    def respond(self, txn: int, unit: int, body: bytes) -> bytes:
        devices: dict[int, Device] = self.server.devices
        function = body[0]

        if function != READ_HOLDING_REGISTERS:
            return error(txn, unit, function, ILLEGAL_FUNCTION)

        start, count = struct.unpack(">HH", body[1:5])
        if not 1 <= count <= MAX_REGISTERS:
            return error(txn, unit, function, ILLEGAL_DATA_ADDRESS)

        device = devices.get(unit)
        if device is None:
            # A real bus is silent for an address nobody answers to, and the
            # master times out. Staying silent is more faithful than replying
            # with an error - and it is what your poller must cope with.
            print(f"  [sim] no device at unit {unit}; staying silent")
            return b""

        values = device.read(start, count)
        payload = struct.pack(">BB", function, 2 * len(values))
        payload += b"".join(struct.pack(">H", v) for v in values)
        return struct.pack(">HHHB", txn, 0, len(payload) + 1, unit) + payload


def error(txn: int, unit: int, function: int, code: int) -> bytes:
    payload = struct.pack(">BB", function | 0x80, code)
    return struct.pack(">HHHB", txn, 0, len(payload) + 1, unit) + payload


def recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    """TCP gives you what it feels like. Modbus needs exactly n bytes."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# --------------------------------------------------------------------------

ROOMS = ["Corridor", "Airlock", "Filling", "Gowning", "Prep", "Wash",
         "Sampling", "Staging"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated Jetpace iDP-100 devices")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--units", type=int, default=3, help="how many devices")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    devices = [Device(i + 1, ROOMS[i % len(ROOMS)]) for i in range(args.units)]

    server = Server((args.host, args.port), Handler)
    server.devices = {d.unit_id: d for d in devices}

    stop = threading.Event()
    threading.Thread(target=simulate, args=(devices, stop), daemon=True).start()

    print(f"iDP-100 simulator on {args.host}:{args.port}")
    for d in devices:
        print(f"  unit {d.unit_id}  {d.room}")
    print("  DP_PV at datasheet 40001 = wire address 0, signed 16-bit")
    print("Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
        stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
