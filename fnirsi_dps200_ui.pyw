#!/usr/bin/env python3
"""
FNIRSI DPS-150 Dual Power Supply Controller
============================================
Controls up to two DPS-150 PSUs with a live UI and MQTT publishing
for Home Assistant integration.

Requirements:
    pip install pyserial paho-mqtt keyring

Usage:
    python fnirsi_dps200_ui.py

Place this file in the same folder as fnirsi_dps200.py.
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import time
import json
import os
import pathlib

CONFIG_FILE = pathlib.Path(__file__).parent / "fnirsi_dps200_config.json"

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}

def save_config(cfg: dict):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass

try:
    import paho.mqtt.client as mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

import ctypes
import ctypes.wintypes
import base64
import sys

# ── Windows DPAPI password encryption ────────────────────────────────────────
# Encrypts using the current Windows user's credentials.
# No extra packages needed — ctypes ships with Python.

class _DPAPI_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

def _dpapi_encrypt(plaintext: str) -> str:
    """Return DPAPI-encrypted base64 string, or '' on failure."""
    if sys.platform != "win32" or not plaintext:
        return ""
    try:
        raw = plaintext.encode("utf-8")
        buf = ctypes.create_string_buffer(raw)
        src = _DPAPI_BLOB(len(raw),
                          ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        dst = _DPAPI_BLOB()
        ok  = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(src), None, None, None, None, 0,
            ctypes.byref(dst))
        if not ok:
            return ""
        result = ctypes.string_at(dst.pbData, dst.cbData)
        ctypes.windll.kernel32.LocalFree(dst.pbData)
        return base64.b64encode(result).decode("ascii")
    except Exception:
        return ""

def _dpapi_decrypt(b64: str) -> str:
    """Decrypt a DPAPI base64 blob back to plaintext."""
    if sys.platform != "win32" or not b64:
        return ""
    try:
        raw = base64.b64decode(b64)
        buf = ctypes.create_string_buffer(raw)
        src = _DPAPI_BLOB(len(raw),
                          ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        dst = _DPAPI_BLOB()
        ok  = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0,
            ctypes.byref(dst))
        if not ok:
            return ""
        result = ctypes.string_at(dst.pbData, dst.cbData)
        ctypes.windll.kernel32.LocalFree(dst.pbData)
        return result.decode("utf-8")
    except Exception:
        return ""

def load_mqtt_password(cfg: dict) -> str:
    blob = cfg.get("mqtt_pass_encrypted", "")
    return _dpapi_decrypt(blob) if blob else ""

def save_mqtt_password(password: str, cfg: dict):
    cfg["mqtt_pass_encrypted"] = _dpapi_encrypt(password)
    cfg.pop("mqtt_pass", None)   # remove any old plaintext entry

try:
    from fnirsi_dps200 import DPS200
    DRIVER_AVAILABLE = True
except ImportError:
    DRIVER_AVAILABLE = False

POLL_INTERVAL = 0.5   # seconds between readings

PROTECTION_LABELS = ["Normal", "OVP", "OCP", "OPP", "OTP", "LVP", "REP"]

# ─── Colors ─────────────────────────────────────────────────────────────────

CLR_ON      = "#27ae60"   # green  — output enabled
CLR_OFF     = "#c0392b"   # red    — output disabled
CLR_NEUTRAL = "#2c3e50"   # dark   — default button
CLR_FG      = "white"


# ─── PSU controller (background thread) ─────────────────────────────────────

class PSUController:
    """Manages a single DPS-150 connection in a background thread."""

    def __init__(self, psu_id: int, on_data, on_status):
        self.psu_id   = psu_id
        self.on_data   = on_data     # (psu_id, dict)
        self.on_status = on_status   # (psu_id, str, bool)
        self.psu       = None
        self.connected = False
        self._cmd_q    = queue.Queue()
        self._stop     = threading.Event()
        self._thread   = None

    def connect(self, port: str):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(port,), daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop.set()

    def send(self, cmd: str, value=None):
        """Queue a command: 'voltage', 'current', 'output' (True/False)."""
        self._cmd_q.put((cmd, value))

    def _run(self, port: str):
        self.on_status(self.psu_id, f"Connecting to {port}…", False)
        try:
            self.psu = DPS200(port, timeout=1.0)
            self.psu.connect()
            self.connected = True
            self.on_status(self.psu_id, f"Connected  ({port})", True)
        except Exception as e:
            self.on_status(self.psu_id, f"Error: {e}", False)
            return

        while not self._stop.is_set():
            # Drain command queue first
            while not self._cmd_q.empty():
                try:
                    cmd, val = self._cmd_q.get_nowait()
                    if cmd == "voltage":
                        self.psu.set_voltage(val)
                    elif cmd == "current":
                        self.psu.set_current_limit(val)
                    elif cmd == "output":
                        self.psu.output_on() if val else self.psu.output_off()
                except Exception:
                    pass

            # Poll
            try:
                data = self.psu.poll()
                if "error" not in data:
                    self.on_data(self.psu_id, data)
                else:
                    self.on_status(self.psu_id,
                                   f"Poll error: {data['error']}", True)
            except Exception as e:
                self.on_status(self.psu_id, f"Lost connection: {e}", False)
                break

            time.sleep(POLL_INTERVAL)

        try:
            self.psu.disconnect()
        except Exception:
            pass
        self.connected = False
        if not self._stop.is_set():
            self.on_status(self.psu_id, "Disconnected", False)
        else:
            self.on_status(self.psu_id, "Disconnected", False)


# ─── MQTT manager ────────────────────────────────────────────────────────────

class MQTTManager:
    """Publishes PSU readings to an MQTT broker and relays commands back."""

    def __init__(self, on_status, on_command):
        self.on_status  = on_status   # (str, bool)
        self.on_command = on_command  # (psu_id, cmd, value)
        self.client     = None
        self.connected  = False
        self._host      = ""
        self._port      = 1883

    # ── Connection ────────────────────────────────────────────────────────

    def connect(self, host: str, port: int,
                username: str = "", password: str = ""):
        if not MQTT_AVAILABLE:
            self.on_status("paho-mqtt not installed — pip install paho-mqtt",
                           False)
            return

        self._host = host
        self._port = port

        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

        # Support both paho-mqtt v1 and v2
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION1,
                client_id="fnirsi_dps200_ui")
        except AttributeError:
            self.client = mqtt.Client(client_id="fnirsi_dps200_ui")

        if username:
            self.client.username_pw_set(username, password or None)

        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

        self.on_status("Connecting…", False)
        try:
            self.client.connect_async(host, port, keepalive=60)
            self.client.loop_start()
        except Exception as e:
            self.on_status(f"Error: {e}", False)

    def disconnect(self):
        if self.client:
            self.client.loop_stop()
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.connected = False

    # ── Publish ───────────────────────────────────────────────────────────

    def publish_state(self, psu_id: int, data: dict):
        if not self.connected or not self.client:
            return
        payload = json.dumps({
            k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in data.items()
        })
        self.client.publish(
            f"dps150/psu{psu_id}/state", payload, retain=True)

    # ── HA MQTT Discovery ─────────────────────────────────────────────────

    def publish_discovery(self):
        if not self.connected or not self.client:
            return

        for psu_id in [1, 2]:
            uid     = f"dps200_psu{psu_id}"
            state_t = f"dps150/psu{psu_id}/state"
            device  = {
                "identifiers":  [uid],
                "name":         f"FNIRSI DPS-150 PSU {psu_id}",
                "model":        "DPS-150",
                "manufacturer": "FNIRSI",
            }

            sensors = [
                ("output_voltage_V",  f"PSU {psu_id} Output Voltage",
                 "V",    "voltage",     "measurement"),
                ("output_current_A",  f"PSU {psu_id} Output Current",
                 "A",    "current",     "measurement"),
                ("output_power_W",    f"PSU {psu_id} Output Power",
                 "W",    "power",       "measurement"),
                ("input_voltage_V",   f"PSU {psu_id} Input Voltage",
                 "V",    "voltage",     "measurement"),
                ("temperature_C",     f"PSU {psu_id} Temperature",
                 "°C",   "temperature", "measurement"),
                ("capacity_Ah",       f"PSU {psu_id} Capacity",
                 "Ah",   None,          "total_increasing"),
                ("energy_Wh",         f"PSU {psu_id} Energy",
                 "Wh",   "energy",      "total_increasing"),
            ]

            for field, name, unit, dev_class, state_class in sensors:
                cfg = {
                    "name":                       name,
                    "state_topic":                state_t,
                    "value_template":             f"{{{{ value_json.{field} | round(2) }}}}",
                    "unit_of_measurement":        unit,
                    "state_class":                state_class,
                    "suggested_display_precision": 2,
                    "unique_id":                  f"{uid}_{field}",
                    "device":                     device,
                }
                if dev_class:
                    cfg["device_class"] = dev_class
                self.client.publish(
                    f"homeassistant/sensor/{uid}_{field}/config",
                    json.dumps(cfg), retain=True)

            # Number entities — voltage and current setpoints (readable + settable)
            numbers = [
                ("set_voltage_V",  f"PSU {psu_id} Set Voltage",
                 "V",  "voltage", "command/voltage", 0, 30,  0.001),
                ("set_current_A",  f"PSU {psu_id} Set Current",
                 "A",  "current", "command/current", 0, 20,  0.001),
            ]
            for field, name, unit, dev_class, cmd, mn, mx, step in numbers:
                self.client.publish(
                    f"homeassistant/number/{uid}_{field}/config",
                    json.dumps({
                        "name":                       name,
                        "state_topic":                state_t,
                        "value_template":             f"{{{{ value_json.{field} | round(2) }}}}",
                        "command_topic":              f"dps150/psu{psu_id}/{cmd}",
                        "unit_of_measurement":        unit,
                        "device_class":               dev_class,
                        "min":                        mn,
                        "max":                        mx,
                        "step":                       step,
                        "mode":                       "box",
                        "suggested_display_precision": 2,
                        "unique_id":                  f"{uid}_{field}",
                        "device":                     device,
                    }), retain=True)

            # Switch — output enable
            self.client.publish(
                f"homeassistant/switch/{uid}_output/config",
                json.dumps({
                    "name":           f"PSU {psu_id} Output",
                    "state_topic":    state_t,
                    "value_template": "{{ 'ON' if value_json.output_enabled else 'OFF' }}",
                    "command_topic":  f"dps150/psu{psu_id}/command/output",
                    "payload_on":     "ON",
                    "payload_off":    "OFF",
                    "device_class":   "switch",
                    "unique_id":      f"{uid}_output",
                    "device":         device,
                }), retain=True)

            # Binary sensor — PSU serial connection state
            self.client.publish(
                f"homeassistant/binary_sensor/{uid}_connected/config",
                json.dumps({
                    "name":         f"PSU {psu_id} Connected",
                    "state_topic":  f"dps150/psu{psu_id}/connected",
                    "payload_on":   "ON",
                    "payload_off":  "OFF",
                    "device_class": "connectivity",
                    "unique_id":    f"{uid}_connected",
                    "device":       device,
                }), retain=True)

            # Text entity — COM port
            self.client.publish(
                f"homeassistant/text/{uid}_port/config",
                json.dumps({
                    "name":          f"PSU {psu_id} COM Port",
                    "state_topic":   f"dps150/psu{psu_id}/port",
                    "command_topic": f"dps150/psu{psu_id}/command/port",
                    "pattern":       "^COM\\d+$|^/dev/tty\\S+$",
                    "unique_id":     f"{uid}_port",
                    "device":        device,
                }), retain=True)

            # Button — connect
            self.client.publish(
                f"homeassistant/button/{uid}_connect/config",
                json.dumps({
                    "name":          f"PSU {psu_id} Connect",
                    "command_topic": f"dps150/psu{psu_id}/command/connect",
                    "payload_press": "PRESS",
                    "unique_id":     f"{uid}_connect",
                    "device":        device,
                }), retain=True)

            # Button — disconnect
            self.client.publish(
                f"homeassistant/button/{uid}_disconnect/config",
                json.dumps({
                    "name":          f"PSU {psu_id} Disconnect",
                    "command_topic": f"dps150/psu{psu_id}/command/disconnect",
                    "payload_press": "PRESS",
                    "unique_id":     f"{uid}_disconnect",
                    "device":        device,
                }), retain=True)

    # ── Callbacks ─────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            self.on_status(f"Connected  {self._host}:{self._port}", True)
            for psu_id in [1, 2]:
                client.subscribe(f"dps150/psu{psu_id}/command/#")
            self.publish_discovery()
        else:
            self.on_status(f"Connection refused (rc={rc})", False)

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        self.on_status("Disconnected", False)

    def publish_port(self, psu_id: int, port: str):
        """Publish the current COM port so the HA text entity stays in sync."""
        if self.connected and self.client:
            self.client.publish(
                f"dps150/psu{psu_id}/port", port, retain=True)

    def publish_connection_state(self, psu_id: int, connected: bool):
        """Publish PSU connection state as ON/OFF."""
        if self.connected and self.client:
            self.client.publish(
                f"dps150/psu{psu_id}/connected",
                "ON" if connected else "OFF", retain=True)

    def _on_message(self, client, userdata, msg):
        # dps150/psu{id}/command/{cmd}
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[2] != "command":
            return
        try:
            psu_id = int(parts[1].replace("psu", ""))
            cmd    = parts[3]
            val    = msg.payload.decode("utf-8").strip()
            if cmd == "voltage":
                self.on_command(psu_id, "voltage", float(val))
            elif cmd == "current":
                self.on_command(psu_id, "current", float(val))
            elif cmd == "output":
                self.on_command(psu_id, "output", val.upper() == "ON")
            elif cmd == "port":
                self.on_command(psu_id, "port", val)
            elif cmd == "connect":
                # payload may be a port string or just "PRESS" (button entity)
                self.on_command(psu_id, "connect",
                                val if val.upper() != "PRESS" else None)
            elif cmd == "disconnect":
                self.on_command(psu_id, "disconnect", None)
        except (ValueError, IndexError):
            pass


# ─── PSU panel widget ────────────────────────────────────────────────────────

class PSUPanel(tk.LabelFrame):
    """UI panel for a single DPS-150."""

    def __init__(self, parent, psu_id: int,
                 on_connect, on_disconnect, on_command, config: dict = {}):
        super().__init__(parent,
                         text=f"  PSU {psu_id}  ",
                         font=("Segoe UI", 10, "bold"),
                         padx=12, pady=8)
        self.psu_id       = psu_id
        self._on_connect  = on_connect
        self._on_disconnect = on_disconnect
        self._on_command  = on_command
        self._connected   = False
        self._output_on   = False
        self._config      = config
        self._build()

    def _build(self):
        # ── Connection row ────────────────────────────────────────────────
        cf = tk.Frame(self)
        cf.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        tk.Label(cf, text="COM Port:", font=("Segoe UI", 9)).pack(side="left")
        default_port = self._config.get(f"psu{self.psu_id}_port",
                                        f"COM{6 + self.psu_id}")
        self._port_var = tk.StringVar(value=default_port)
        tk.Entry(cf, textvariable=self._port_var, width=7,
                 font=("Segoe UI", 9)).pack(side="left", padx=4)
        self._conn_btn = tk.Button(
            cf, text="Connect", width=10, font=("Segoe UI", 9),
            bg=CLR_NEUTRAL, fg=CLR_FG, relief="flat",
            command=self._toggle_connect)
        self._conn_btn.pack(side="left", padx=4)
        self._status_lbl = tk.Label(
            cf, text="Disconnected", font=("Segoe UI", 9), fg="gray")
        self._status_lbl.pack(side="left", padx=8)

        # ── Readings ──────────────────────────────────────────────────────
        rf = tk.LabelFrame(self, text=" Readings ", font=("Segoe UI", 9),
                           padx=8, pady=6)
        rf.grid(row=1, column=0, sticky="ew", pady=(0, 10))

        self._vars = {}
        fields = [
            ("input_voltage_V",  "Input Voltage",  "V"),
            ("output_voltage_V", "Output Voltage", "V"),
            ("output_current_A", "Current",        "A"),
            ("output_power_W",   "Power",          "W"),
            ("temperature_C",    "Temperature",    "°C"),
            ("mode",             "Mode",           ""),
            ("protection_state", "Protection",     ""),
        ]
        for i, (key, label, unit) in enumerate(fields):
            tk.Label(rf, text=f"{label}:", font=("Segoe UI", 9),
                     anchor="w", width=16).grid(row=i, column=0, sticky="w")
            var = tk.StringVar(value="--")
            tk.Label(rf, textvariable=var, font=("Courier New", 10, "bold"),
                     width=10, anchor="e").grid(row=i, column=1, sticky="e")
            tk.Label(rf, text=unit, font=("Segoe UI", 9),
                     width=3, anchor="w").grid(row=i, column=2, sticky="w")
            self._vars[key] = var

        # ── Controls ──────────────────────────────────────────────────────
        ctf = tk.LabelFrame(self, text=" Controls ", font=("Segoe UI", 9),
                            padx=8, pady=8)
        ctf.grid(row=2, column=0, sticky="ew")

        # Voltage row
        tk.Label(ctf, text="Set Voltage (V):", font=("Segoe UI", 9),
                 anchor="w", width=16).grid(row=0, column=0, sticky="w", pady=3)
        self._v_entry = tk.Entry(ctf, width=10, font=("Segoe UI", 9))
        self._v_entry.insert(0, "5.000")
        self._v_entry.grid(row=0, column=1, padx=4)
        tk.Button(ctf, text="Set", width=6, font=("Segoe UI", 9),
                  bg=CLR_NEUTRAL, fg=CLR_FG, relief="flat",
                  command=self._set_voltage).grid(row=0, column=2)

        # Current row
        tk.Label(ctf, text="Set Current (A):", font=("Segoe UI", 9),
                 anchor="w", width=16).grid(row=1, column=0, sticky="w", pady=3)
        self._a_entry = tk.Entry(ctf, width=10, font=("Segoe UI", 9))
        self._a_entry.insert(0, "1.000")
        self._a_entry.grid(row=1, column=1, padx=4)
        tk.Button(ctf, text="Set", width=6, font=("Segoe UI", 9),
                  bg=CLR_NEUTRAL, fg=CLR_FG, relief="flat",
                  command=self._set_current).grid(row=1, column=2)

        # Output toggle
        self._out_btn = tk.Button(
            ctf, text="Enable Output", font=("Segoe UI", 10, "bold"),
            width=18, bg=CLR_OFF, fg=CLR_FG, relief="flat",
            command=self._toggle_output)
        self._out_btn.grid(row=2, column=0, columnspan=3, pady=(10, 2))

    # ── Internal handlers ─────────────────────────────────────────────────

    def _toggle_connect(self):
        if not self._connected:
            port = self._port_var.get().strip()
            if port:
                self._on_connect(self.psu_id, port)
        else:
            self._on_disconnect(self.psu_id)

    def _set_voltage(self):
        try:
            v = float(self._v_entry.get())
            self._on_command(self.psu_id, "voltage", v)
        except ValueError:
            messagebox.showerror("Invalid input", "Enter a number, e.g. 5.0")

    def _set_current(self):
        try:
            a = float(self._a_entry.get())
            self._on_command(self.psu_id, "current", a)
        except ValueError:
            messagebox.showerror("Invalid input", "Enter a number, e.g. 1.0")

    def _toggle_output(self):
        self._on_command(self.psu_id, "output", not self._output_on)

    def get_port(self) -> str:
        return self._port_var.get().strip()

    def set_port(self, port: str):
        """Update the COM port field (called from MQTT command)."""
        self._port_var.set(port.strip())

    # ── Public update API ─────────────────────────────────────────────────

    def update_status(self, status: str, connected: bool):
        self._connected = connected
        self._status_lbl.config(
            text=status,
            fg="green" if connected else
               ("red" if "Error" in status or "Lost" in status else "gray"))
        self._conn_btn.config(
            text="Disconnect" if connected else "Connect",
            bg="#7f8c8d" if connected else CLR_NEUTRAL)
        if not connected:
            for var in self._vars.values():
                var.set("--")
            self._output_on = False
            self._out_btn.config(text="Enable Output", bg=CLR_OFF)

    def update_data(self, data: dict):
        self._output_on = data.get("output_enabled", False)

        fmts = {
            "input_voltage_V":  f"{data.get('input_voltage_V', 0):8.3f}",
            "output_voltage_V": f"{data.get('output_voltage_V', 0):8.3f}",
            "output_current_A": f"{data.get('output_current_A', 0):8.4f}",
            "output_power_W":   f"{data.get('output_power_W', 0):8.3f}",
            "temperature_C":    f"{data.get('temperature_C', 0):8.1f}",
            "mode":             data.get("mode", "--"),
            "protection_state": data.get("protection_state", "--"),
        }
        for key, var in self._vars.items():
            if key in fmts:
                var.set(fmts[key])

        # Sync setpoint entries if user hasn't typed in them recently
        # (only when connected and fields are blank/default)
        sv = data.get("set_voltage_V")
        sa = data.get("set_current_A")
        if sv is not None and self._v_entry.get() in ("", "5.000"):
            self._v_entry.delete(0, "end")
            self._v_entry.insert(0, f"{sv:.3f}")
        if sa is not None and self._a_entry.get() in ("", "1.000"):
            self._a_entry.delete(0, "end")
            self._a_entry.insert(0, f"{sa:.3f}")

        self._out_btn.config(
            text="Disable Output" if self._output_on else "Enable Output",
            bg=CLR_ON if self._output_on else CLR_OFF)


# ─── MQTT panel widget ───────────────────────────────────────────────────────

class MQTTPanel(tk.LabelFrame):
    """MQTT broker connection bar."""

    def __init__(self, parent, on_connect, on_disconnect, config: dict = {}):
        super().__init__(parent,
                         text="  MQTT → Home Assistant  ",
                         font=("Segoe UI", 10, "bold"),
                         padx=12, pady=8)
        self._on_connect    = on_connect
        self._on_disconnect = on_disconnect
        self._connected     = False
        self._config        = config
        self._build()

    def _build(self):
        row = tk.Frame(self)
        row.pack(fill="x")

        def lbl(text):
            tk.Label(row, text=text, font=("Segoe UI", 9)).pack(side="left")

        lbl("Broker:")
        self._host_var = tk.StringVar(
            value=self._config.get("mqtt_host", "homeassistant.local"))
        tk.Entry(row, textvariable=self._host_var, width=22,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 8))

        lbl("Port:")
        self._port_var = tk.StringVar(
            value=str(self._config.get("mqtt_port", 1883)))
        tk.Entry(row, textvariable=self._port_var, width=6,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 8))

        lbl("User:")
        self._user_var = tk.StringVar(
            value=self._config.get("mqtt_user", ""))
        tk.Entry(row, textvariable=self._user_var, width=12,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 4))

        lbl("Pass:")
        self._pass_var = tk.StringVar(
            value=load_mqtt_password(self._config))
        tk.Entry(row, textvariable=self._pass_var, show="*", width=12,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 8))

        self._conn_btn = tk.Button(
            row, text="Connect", width=10, font=("Segoe UI", 9),
            bg=CLR_NEUTRAL, fg=CLR_FG, relief="flat",
            command=self._toggle)
        self._conn_btn.pack(side="left", padx=4)

        self._status_lbl = tk.Label(
            row, text="Not connected", font=("Segoe UI", 9), fg="gray")
        self._status_lbl.pack(side="left", padx=8)

    def _toggle(self):
        if not self._connected:
            host = self._host_var.get().strip()
            try:
                port = int(self._port_var.get())
            except ValueError:
                port = 1883
            password = self._pass_var.get()
            self._on_connect(host, port,
                             self._user_var.get().strip(),
                             password)
        else:
            self._on_disconnect()

    def get_password(self) -> str:
        return self._pass_var.get()

    def get_settings(self) -> dict:
        try:
            port = int(self._port_var.get())
        except ValueError:
            port = 1883
        return {
            "mqtt_host": self._host_var.get().strip(),
            "mqtt_port": port,
            "mqtt_user": self._user_var.get().strip(),
        }

    def update_status(self, status: str, connected: bool):
        self._connected = connected
        self._status_lbl.config(
            text=status,
            fg="green" if connected else
               ("red" if "Error" in status or "refused" in status else "gray"))
        self._conn_btn.config(
            text="Disconnect" if connected else "Connect",
            bg="#7f8c8d" if connected else CLR_NEUTRAL)


# ─── System tray support ─────────────────────────────────────────────────────

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

def _make_tray_icon():
    """Create a simple coloured square as the tray icon."""
    img = Image.new("RGB", (64, 64), color="#2c3e50")
    d = ImageDraw.Draw(img)
    d.rectangle([8, 8, 56, 56], fill="#27ae60")
    d.text((18, 20), "PS", fill="white")
    return img


# ─── Main application ────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FNIRSI DPS-150 Controller")
        self.resizable(False, False)
        self.configure(bg="#ecf0f1")

        self._controllers: dict[int, PSUController] = {}
        self._panels:      dict[int, PSUPanel]       = {}
        self._mqtt:        MQTTManager | None        = None
        self._q           = queue.Queue()
        self._tray        = None
        self._cfg         = load_config()

        if not DRIVER_AVAILABLE:
            messagebox.showerror(
                "Missing driver",
                "fnirsi_dps200.py not found.\n"
                "Place it in the same folder as this file.")
            self.destroy()
            return

        self._build_ui()
        self._poll_queue()
        self._setup_tray()
        self._auto_connect_mqtt()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 8}

        # ── PSU count selector ────────────────────────────────────────────
        top_bar = tk.Frame(self, bg="#ecf0f1")
        top_bar.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(top_bar, text="Power Supplies:",
                 font=("Segoe UI", 9), bg="#ecf0f1").pack(side="left")
        self._num_psus_var = tk.IntVar(
            value=self._cfg.get("num_psus", 2))
        for n in [1, 2]:
            tk.Radiobutton(
                top_bar, text=str(n), variable=self._num_psus_var, value=n,
                font=("Segoe UI", 9), bg="#ecf0f1",
                command=self._on_num_psus_changed,
            ).pack(side="left", padx=4)

        # ── PSU panels side by side ───────────────────────────────────────
        self._psu_row = tk.Frame(self, bg="#ecf0f1")
        self._psu_row.pack(fill="both", expand=True, **pad)

        for psu_id in [1, 2]:
            panel = PSUPanel(
                self._psu_row, psu_id,
                on_connect=self._psu_connect,
                on_disconnect=self._psu_disconnect,
                on_command=self._psu_command,
                config=self._cfg,
            )
            self._panels[psu_id] = panel

        self._apply_num_psus(self._num_psus_var.get())

        # MQTT panel
        if MQTT_AVAILABLE:
            self._mqtt = MQTTManager(
                on_status=self._on_mqtt_status,
                on_command=self._on_mqtt_command,
            )
        mqtt_panel = MQTTPanel(
            self,
            on_connect=self._mqtt_connect,
            on_disconnect=self._mqtt_disconnect,
            config=self._cfg,
        )
        mqtt_panel.pack(fill="x", padx=10, pady=(0, 10))
        self._mqtt_panel = mqtt_panel

        if not MQTT_AVAILABLE:
            mqtt_panel.update_status(
                "paho-mqtt not installed — run: pip install paho-mqtt", False)

    def _apply_num_psus(self, n: int):
        """Show/hide PSU panels based on count and repack."""
        for psu_id in [1, 2]:
            self._panels[psu_id].pack_forget()
        for psu_id in range(1, n + 1):
            self._panels[psu_id].pack(
                side="left", fill="both", expand=True,
                padx=(0 if psu_id == 1 else 8, 0),
                in_=self._psu_row)
        # Hide/disconnect PSU 2 if switching to 1
        if n < 2:
            self._psu_disconnect(2)

    def _on_num_psus_changed(self):
        n = self._num_psus_var.get()
        self._apply_num_psus(n)
        self._cfg["num_psus"] = n
        save_config(self._cfg)

    def _auto_connect_mqtt(self):
        """Connect to MQTT on startup if a host is saved in config."""
        host = self._cfg.get("mqtt_host", "").strip()
        if not host or not MQTT_AVAILABLE:
            return
        s = self._mqtt_panel.get_settings()
        pw = load_mqtt_password(self._cfg)
        self.after(500, lambda: self._mqtt_connect(
            s["mqtt_host"], s["mqtt_port"],
            s["mqtt_user"], pw))

    def _setup_tray(self):
        if not TRAY_AVAILABLE:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray = pystray.Icon(
            "fnirsi_dps200",
            _make_tray_icon(),
            "FNIRSI DPS-150 Controller",
            menu,
        )
        threading.Thread(target=self._tray.run, daemon=True).start()
        # Minimise to tray on window close button → use WM_DELETE_WINDOW
        # (set in __main__ block, overridden below)

    def _tray_show(self, icon=None, item=None):
        self.after(0, self.deiconify)
        self.after(0, self.lift)

    def _tray_quit(self, icon=None, item=None):
        self.after(0, self._shutdown)

    # ── PSU wiring ────────────────────────────────────────────────────────

    def _psu_connect(self, psu_id: int, port: str):
        if psu_id in self._controllers:
            self._controllers[psu_id].disconnect()
        ctrl = PSUController(psu_id,
                             on_data=self._on_psu_data,
                             on_status=self._on_psu_status)
        self._controllers[psu_id] = ctrl
        ctrl.connect(port)

    def _psu_disconnect(self, psu_id: int):
        if psu_id in self._controllers:
            self._controllers[psu_id].disconnect()

    def _psu_command(self, psu_id: int, cmd: str, value=None):
        ctrl = self._controllers.get(psu_id)
        if ctrl and ctrl.connected:
            ctrl.send(cmd, value)

    # ── MQTT wiring ───────────────────────────────────────────────────────

    def _mqtt_connect(self, host, port, user, pw):
        if self._mqtt:
            self._mqtt.connect(host, port, user, pw)

    def _mqtt_disconnect(self):
        if self._mqtt:
            self._mqtt.disconnect()

    # ── Callbacks from background threads (thread-safe via queue) ─────────

    def _on_psu_data(self, psu_id: int, data: dict):
        self._q.put(("data", psu_id, data))

    def _on_psu_status(self, psu_id: int, status: str, connected: bool):
        self._q.put(("psu_status", psu_id, status, connected))

    def _on_mqtt_status(self, status: str, connected: bool):
        self._q.put(("mqtt_status", status, connected))

    def _on_mqtt_command(self, psu_id: int, cmd: str, value):
        if cmd == "port":
            # Update the port field in the UI and publish state back
            panel = self._panels.get(psu_id)
            if panel:
                panel.set_port(value)
                if self._mqtt:
                    self._mqtt.publish_port(psu_id, value)
        elif cmd == "connect":
            panel = self._panels.get(psu_id)
            if panel:
                port = value if value else panel.get_port()
                if port:
                    self._psu_connect(psu_id, port)
        elif cmd == "disconnect":
            self._psu_disconnect(psu_id)
        else:
            # voltage / current / output — route to PSU controller
            self._psu_command(psu_id, cmd, value)

    # ── UI queue processor ────────────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                item = self._q.get_nowait()
                if item[0] == "data":
                    _, psu_id, data = item
                    self._panels[psu_id].update_data(data)
                    if self._mqtt:
                        self._mqtt.publish_state(psu_id, data)
                elif item[0] == "psu_status":
                    _, psu_id, status, connected = item
                    self._panels[psu_id].update_status(status, connected)
                    if self._mqtt:
                        self._mqtt.publish_connection_state(psu_id, connected)
                        if connected:
                            port = self._panels[psu_id].get_port()
                            self._mqtt.publish_port(psu_id, port)
                elif item[0] == "mqtt_status":
                    _, status, connected = item
                    self._mqtt_panel.update_status(status, connected)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    # ── Shutdown ──────────────────────────────────────────────────────────

    def _save_config(self):
        cfg = {}
        for psu_id, panel in self._panels.items():
            cfg[f"psu{psu_id}_port"] = panel.get_port()
        cfg.update(self._mqtt_panel.get_settings())
        save_mqtt_password(self._mqtt_panel.get_password(), cfg)
        cfg["num_psus"] = self._num_psus_var.get()
        save_config(cfg)

    def on_close(self):
        """Called by the window close button (X)."""
        if TRAY_AVAILABLE and self._tray is not None:
            # Minimise to tray instead of quitting
            self.withdraw()
        else:
            self._shutdown()

    def _shutdown(self):
        """Save config, stop everything, destroy the window."""
        self._save_config()
        for ctrl in self._controllers.values():
            ctrl.disconnect()
        if self._mqtt:
            self._mqtt.disconnect()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
        self.after(300, self.destroy)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
