# FNIRSI DPS-150 Controller

A Python desktop application for controlling up to two FNIRSI DPS-150 programmable power supplies over USB serial, with a UI and full Home Assistant integration via MQTT.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![Platform](https://img.shields.io/badge/platform-Windows-lightgrey) ![License](https://img.shields.io/badge/license-MIT-green)

> **Built upon** the open-source [DPS-150 Python library](https://github.com/KochC/DPS-150-python-library) by KochC, which provided the confirmed serial protocol for the DPS-150.

---

## Features

- Control 1 or 2 DPS-150 units simultaneously
- Set output voltage and current limit
- Enable / disable output
- Live readings: voltage, current, power, temperature, input voltage, mode (CC/CV), protection state, accumulated Ah/Wh
- Settings (COM ports, MQTT) saved between sessions
- MQTT password encrypted with Windows DPAPI — never stored in plaintext
- Full Home Assistant MQTT discovery — entities appear automatically
- System tray support — minimise to tray, restore or quit from the tray icon
- Runs silently with no console window (`.pyw`)

---

## Requirements

- Python 3.10+
- FNIRSI DPS-150 connected via USB (appears as a COM port)
- Home Assistant with an MQTT broker (optional)

Install dependencies:

```
pip install -r requirements.txt
```

Dependencies: `pyserial`, `paho-mqtt`, `pystray`, `pillow`. No extra packages are needed for password encryption — Windows DPAPI is used via Python's built-in `ctypes`.

---

## Files

| File | Description |
|---|---|
| `fnirsi_dps200.py` | DPS-150 serial driver |
| `fnirsi_dps200_ui.pyw` | Main UI application |
| `requirements.txt` | Python dependencies |

Both files must be in the same folder.

---

## Quick Start

1. Plug in your DPS-150 and note the COM port (Device Manager → Ports)
2. Run `fnirsi_dps200_ui.pyw` (double-click or `pythonw fnirsi_dps200_ui.pyw`)
3. Enter the COM port and click **Connect**
4. Optionally enter your MQTT broker details and click **Connect** — HA entities appear automatically

---

## Serial Protocol

The DPS-150 communicates over USB serial at **115200 baud, 8N1 with hardware RTS/CTS flow control**. Without RTS/CTS the device returns no data.

### Packet format

```
Outgoing: [0xF1, CMD, TYPE, LEN, DATA..., CHKSUM]
Incoming: [0xF0, CMD, TYPE, LEN, DATA..., CHKSUM]
CHKSUM = (TYPE + LEN + sum(DATA)) & 0xFF
```

### Key commands

| Command | Type code | Payload |
|---|---|---|
| Init (required on connect) | `0xC1` | `0x01` |
| Get all state | `0xFF` | — |
| Set voltage | `0xC1` | IEEE 754 float, little-endian |
| Set current limit | `0xC2` | IEEE 754 float, little-endian |
| Output enable/disable | `0xDB` | `0x01` / `0x00` |
| Metering enable/disable | `0xD8` | `0x01` / `0x00` |

The init command must be sent before the device will respond to anything else.

### Response payload offsets (139-byte response to `0xFF`)

| Offset | Field |
|---|---|
| 0–3 | Input voltage (float) |
| 4–7 | Set voltage (float) |
| 8–11 | Set current limit (float) |
| 12–15 | Output voltage (float) |
| 16–19 | Output current (float) |
| 20–23 | Output power (float) |
| 24–27 | Temperature °C (float) |
| 76–79 | OVP threshold (float) |
| 80–83 | OCP threshold (float) |
| 84–87 | OPP threshold (float) |
| 99–102 | Capacity Ah (float) |
| 103–106 | Energy Wh (float) |
| 107 | Output enabled (byte, 1=on) |
| 108 | Protection state (byte, 0=Normal) |
| 109 | Mode (byte, 0=CC / 1=CV) |

---

## Home Assistant Integration

The app publishes MQTT discovery messages automatically when it connects to the broker. No manual YAML configuration is needed.

### Entities created per PSU

| Entity | Type | Description |
|---|---|---|
| Output Voltage | Sensor | Measured output voltage (V) |
| Output Current | Sensor | Measured output current (A) |
| Output Power | Sensor | Calculated output power (W) |
| Input Voltage | Sensor | Supply input voltage (V) |
| Temperature | Sensor | PSU temperature (°C) |
| Capacity | Sensor | Accumulated capacity (Ah) |
| Energy | Sensor | Accumulated energy (Wh) |
| Set Voltage | Number | Voltage setpoint — read + write (V) |
| Set Current | Number | Current limit — read + write (A) |
| Output | Switch | Enable / disable output |
| Connected | Binary Sensor | Serial connection state |
| COM Port | Text | View or set the COM port remotely |
| Connect | Button | Initiate serial connection |
| Disconnect | Button | Drop serial connection |

All numeric values are displayed and published to **2 decimal places**.

### MQTT topics

```
dps150/psu{n}/state              ← JSON payload published every 0.5 s
dps150/psu{n}/connected          ← ON / OFF
dps150/psu{n}/port               ← current COM port string

dps150/psu{n}/command/voltage    → set voltage (e.g. 5.0)
dps150/psu{n}/command/current    → set current limit (e.g. 1.0)
dps150/psu{n}/command/output     → ON / OFF
dps150/psu{n}/command/port       → set COM port (e.g. COM7)
dps150/psu{n}/command/connect    → PRESS (or a port string to connect to a specific port)
dps150/psu{n}/command/disconnect → PRESS
```

### State payload example

```json
{
  "input_voltage_V": 12.30,
  "set_voltage_V": 5.00,
  "set_current_A": 1.00,
  "output_voltage_V": 5.00,
  "output_current_A": 0.52,
  "output_power_W": 2.60,
  "temperature_C": 32.10,
  "capacity_Ah": 0.00,
  "energy_Wh": 0.00,
  "output_enabled": true,
  "mode": "CV",
  "protection_state": "Normal"
}
```

---

## Driver API

```python
from fnirsi_dps200 import DPS200

with DPS200("COM7") as psu:
    psu.set_voltage(5.0)
    psu.set_current_limit(1.0)
    psu.output_on()

    m = psu.poll()
    print(f"{m['output_voltage_V']:.2f} V  {m['output_current_A']:.2f} A")

    psu.output_off()
```

### CLI

```
python fnirsi_dps200.py --port COM7 --read
python fnirsi_dps200.py --port COM7 --set-voltage 5.0 --set-current 1.0 --enable
python fnirsi_dps200.py --port COM7 --monitor --interval 0.5
```

---

## Protocol Notes

The protocol was reverse-engineered from the official FNIRSI Windows application and confirmed against the open-source [DPS-150 Python library](https://github.com/KochC/DPS-150-python-library) by KochC. Key findings:

- **RTS/CTS hardware flow control is mandatory** — without it the device returns nothing at any baud rate
- All numeric values are **IEEE 754 single-precision floats** (not integer millivolts as initially assumed)
- The device sends unsolicited 12-byte V/I/P packets periodically; the driver filters these out when polling for the full state response
- The init command (`CMD 0xC1`) must be the first thing sent after opening the port

---

## Acknowledgements

- [KochC/DPS-150-python-library](https://github.com/KochC/DPS-150-python-library) — confirmed protocol constants, RTS/CTS requirement, and IEEE 754 float encoding used in this project
