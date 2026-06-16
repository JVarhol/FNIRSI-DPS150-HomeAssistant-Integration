"""
FNIRSI DPS-200 Power Supply Driver
===================================
Protocol confirmed by live capture. Matches FNIRSI DPS-150 protocol.

Packet format:
    Outgoing: [0xF1, CMD, TYPE, LEN, DATA..., CHKSUM]
    Incoming: [0xF0, CMD, TYPE, LEN, DATA..., CHKSUM]
    CHKSUM = (TYPE + LEN + sum(DATA)) & 0xFF

Serial: 115200 baud, 8N1, hardware RTS/CTS flow control.
Init command (0xC1) must be sent before any other command.

Usage:
    pip install pyserial
    python fnirsi_dps200.py --port COM7 --read
    python fnirsi_dps200.py --port COM7 --set-voltage 5.0 --enable --monitor
"""

import serial
import struct
import time
import argparse
import sys

# ─── Protocol constants ──────────────────────────────────────────────────────

HEADER_OUT  = 0xF1   # outgoing
HEADER_IN   = 0xF0   # incoming

CMD_GET     = 0xA1   # request value
CMD_SET     = 0xB1   # set value
CMD_INIT    = 0xC1   # connection/init

TYPE_ALL          = 0xFF   # get all state
TYPE_VOLTAGE_SET  = 0xC1   # set target voltage  (float, volts)
TYPE_CURRENT_SET  = 0xC2   # set current limit   (float, amps)
TYPE_OUTPUT_EN    = 0xDB   # output enable/disable (byte: 1/0)
TYPE_METERING_EN  = 0xD8   # metering enable/disable (byte: 1/0)


# ─── Packet helpers ──────────────────────────────────────────────────────────

def _checksum(type_code: int, data: bytes) -> int:
    return (type_code + len(data) + sum(data)) & 0xFF

def build_packet(cmd: int, type_code: int, data: bytes = b"") -> bytes:
    pkt = bytearray([HEADER_OUT, cmd, type_code, len(data)])
    pkt.extend(data)
    pkt.append(_checksum(type_code, data))
    return bytes(pkt)

def verify_packet(pkt: bytes) -> bool:
    if len(pkt) < 5 or pkt[0] != HEADER_IN:
        return False
    n = pkt[3]
    if len(pkt) < 5 + n:
        return False
    return pkt[4 + n] == _checksum(pkt[2], pkt[4:4 + n])


# ─── Pre-built packets ───────────────────────────────────────────────────────

PKT_INIT    = build_packet(CMD_INIT, 0x00, bytes([1]))
PKT_GET_ALL = build_packet(CMD_GET,  TYPE_ALL, b"")


# ─── Driver ──────────────────────────────────────────────────────────────────

class DPS200:
    """
    Driver for the FNIRSI DPS-200 programmable power supply.

    Example
    -------
    with DPS200("/dev/ttyUSB0") as psu:
        psu.set_voltage(5.0)
        psu.set_current_limit(1.0)
        psu.output_on()
        m = psu.poll()
        print(f"{m['output_voltage_V']:.3f} V  {m['output_current_A']:.4f} A")
        psu.output_off()
    """

    def __init__(self, port: str, timeout: float = 1.0):
        self.port = port
        self.timeout = timeout
        self.ser: serial.Serial | None = None

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self) -> "DPS200":
        self.ser = serial.Serial(
            port=self.port,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=True,
            timeout=self.timeout,
        )
        time.sleep(0.3)
        # Send init/connection command — device won't respond without this
        self._send(PKT_INIT)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        return self

    def disconnect(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.disconnect()

    # ── Low-level I/O ─────────────────────────────────────────────────────

    def _send(self, pkt: bytes):
        if not self.ser or not self.ser.is_open:
            raise IOError("Serial port not open")
        self.ser.write(pkt)

    def _read_packet(self, expected_type: int | None = None) -> bytes:
        """Read one complete response packet (starts with 0xF0).

        If expected_type is given, keeps reading and discarding packets until
        one with that type code arrives (or timeout).
        """
        buf = bytearray()
        deadline = time.time() + self.timeout

        while time.time() < deadline:
            chunk = self.ser.read(max(1, self.ser.in_waiting or 1))
            if not chunk:
                continue
            buf.extend(chunk)

            # Discard until 0xF0 sync byte
            while buf and buf[0] != HEADER_IN:
                buf.pop(0)

            if len(buf) >= 4:
                n = buf[3]
                needed = 5 + n   # header(1) cmd(1) type(1) len(1) data(n) chk(1)
                if len(buf) < needed:
                    more = self.ser.read(needed - len(buf))
                    buf.extend(more)
                if len(buf) >= needed:
                    pkt = bytes(buf[:needed])
                    if verify_packet(pkt):
                        if expected_type is None or pkt[2] == expected_type:
                            return pkt
                        # Right format, wrong type — discard and keep looking
                        buf = buf[needed:]
                        continue
                    buf = buf[1:]   # bad checksum, try next byte

        return bytes(buf)

    def _cmd(self, pkt: bytes, want_response: bool = False,
             expected_type: int | None = None) -> bytes:
        self.ser.reset_input_buffer()
        self._send(pkt)
        if want_response:
            return self._read_packet(expected_type)
        time.sleep(0.05)
        return b""

    # ── Public API ────────────────────────────────────────────────────────

    def output_on(self):
        """Enable the output."""
        self._cmd(build_packet(CMD_SET, TYPE_OUTPUT_EN, bytes([1])))

    def output_off(self):
        """Disable the output."""
        self._cmd(build_packet(CMD_SET, TYPE_OUTPUT_EN, bytes([0])))

    def set_voltage(self, volts: float):
        """Set target voltage in volts (e.g. 5.0 for 5 V)."""
        self._cmd(build_packet(CMD_SET, TYPE_VOLTAGE_SET, struct.pack("<f", volts)))

    def set_current_limit(self, amps: float):
        """Set current limit in amps (e.g. 1.0 for 1 A)."""
        self._cmd(build_packet(CMD_SET, TYPE_CURRENT_SET, struct.pack("<f", amps)))

    def metering_on(self):
        """Start energy metering (Ah/Wh counter)."""
        self._cmd(build_packet(CMD_SET, TYPE_METERING_EN, bytes([1])))

    def metering_off(self):
        """Stop energy metering."""
        self._cmd(build_packet(CMD_SET, TYPE_METERING_EN, bytes([0])))

    def poll(self) -> dict:
        """
        Poll the PSU for all measurements and settings.

        Returns a dict with:
            input_voltage_V      — supply input voltage
            output_voltage_V     — measured output voltage
            output_current_A     — measured output current
            output_power_W       — calculated output power
            temperature_C        — PSU temperature
            set_voltage_V        — voltage setpoint
            set_current_A        — current limit setpoint
            output_enabled       — bool, True if output is on
            mode                 — "CC" or "CV"
            protection_state     — "Normal", "OVP", "OCP", etc.
            ovp_V, ocp_A, opp_W  — protection thresholds
            capacity_Ah          — accumulated Ah (metering)
            energy_Wh            — accumulated Wh (metering)
        """
        resp = self._cmd(PKT_GET_ALL, want_response=True, expected_type=TYPE_ALL)
        if len(resp) < 5:
            return {"error": "no response", "raw": resp.hex()}

        payload = resp[4:-1]
        if len(payload) < 119:
            return {"error": f"payload too short ({len(payload)} bytes)", "raw": payload.hex()}

        f = lambda o: struct.unpack_from("<f", payload, o)[0]
        b = lambda o: payload[o]

        PROTECTION = ["Normal", "OVP", "OCP", "OPP", "OTP", "LVP", "REP"]
        pstate = b(108)
        mode   = b(109)

        return {
            "input_voltage_V":   f(0),
            "set_voltage_V":     f(4),
            "set_current_A":     f(8),
            "output_voltage_V":  f(12),
            "output_current_A":  f(16),
            "output_power_W":    f(20),
            "temperature_C":     f(24),
            "ovp_V":             f(76),
            "ocp_A":             f(80),
            "opp_W":             f(84),
            "otp_C":             f(88),
            "lvp_V":             f(92),
            "brightness":        b(96),
            "volume":            b(97),
            "capacity_Ah":       f(99),
            "energy_Wh":         f(103),
            "output_enabled":    bool(b(107)),
            "protection_state":  PROTECTION[pstate] if pstate < len(PROTECTION) else pstate,
            "mode":              "CV" if mode else "CC",
            "upper_limit_V":     f(111),
            "upper_limit_A":     f(115),
        }

    def read_voltage(self) -> float:
        """Return measured output voltage in volts."""
        return self.poll().get("output_voltage_V", float("nan"))

    def read_current(self) -> float:
        """Return measured output current in amps."""
        return self.poll().get("output_current_A", float("nan"))


# ─── CLI ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Control an FNIRSI DPS-200 power supply over USB-serial."
    )
    parser.add_argument("--port", required=True,
                        help="Serial port, e.g. COM7 or /dev/ttyUSB0")
    parser.add_argument("--set-voltage", type=float, metavar="V",
                        help="Set output voltage in volts")
    parser.add_argument("--set-current", type=float, metavar="A",
                        help="Set current limit in amps")
    parser.add_argument("--enable",  action="store_true", help="Enable output")
    parser.add_argument("--disable", action="store_true", help="Disable output")
    parser.add_argument("--read",    action="store_true", help="Print measurements once")
    parser.add_argument("--monitor", action="store_true",
                        help="Continuously print measurements (Ctrl-C to stop)")
    parser.add_argument("--interval", type=float, default=0.5, metavar="SEC",
                        help="Poll interval for --monitor (default 0.5 s)")
    args = parser.parse_args()

    try:
        with DPS200(args.port) as psu:
            print(f"Connected to {args.port} @ 115200 baud")

            if args.set_voltage is not None:
                psu.set_voltage(args.set_voltage)
                print(f"Set voltage → {args.set_voltage:.3f} V")

            if args.set_current is not None:
                psu.set_current_limit(args.set_current)
                print(f"Set current limit → {args.set_current:.4f} A")

            if args.disable:
                psu.output_off()
                print("Output disabled")

            if args.enable:
                psu.output_on()
                print("Output enabled")

            def print_measurement(m: dict):
                if "error" in m:
                    print(f"  Error: {m['error']}")
                    return
                parts = [
                    f"V={m['output_voltage_V']:7.3f}V",
                    f"I={m['output_current_A']:7.4f}A",
                    f"P={m['output_power_W']:6.3f}W",
                    f"T={m['temperature_C']:.1f}C",
                    f"Vset={m['set_voltage_V']:.3f}",
                    f"OUT={'ON' if m['output_enabled'] else 'OFF'}",
                    f"{m['mode']}",
                ]
                print("  ".join(parts))

            if args.read or args.monitor:
                print_measurement(psu.poll())

                if args.monitor:
                    try:
                        while True:
                            time.sleep(args.interval)
                            print_measurement(psu.poll())
                    except KeyboardInterrupt:
                        print("\nStopped.")

    except serial.SerialException as e:
        print(f"Serial error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
