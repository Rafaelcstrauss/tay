import os
import re
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception:  # pragma: no cover
    serial = None
    list_ports = None


class SensorSnapshot(BaseModel):
    light_percent: int
    temperature_c: Optional[float]
    humidity_percent: Optional[float]
    noise_db: Optional[int]
    mode: str
    visual_alert: bool


class ControlRequest(BaseModel):
    value: Optional[int] = None
    mode: Optional[str] = None
    visual_alert: Optional[bool] = None


class SerialManager:
    def __init__(self) -> None:
        self.port: Optional[str] = None
        self.baudrate = 9600
        self.ser: Optional[serial.Serial] = None if serial else None
        self.read_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self.lock = threading.Lock()
        self.last_temperature_c: Optional[float] = None
        self.last_humidity_percent: Optional[float] = None
        self.last_ldr_value: Optional[int] = None

        # State mirrored on microcontroller via commands
        self.current_intensity_percent: int = 50
        self.current_mode: str = "Fria"
        self.current_visual_alert: bool = False

    def _detect_port(self) -> Optional[str]:
        # Allow override via env var
        env_port = os.getenv("ARDUINO_PORT")
        if env_port:
            return env_port
        if not list_ports:
            return None
        candidates = list(list_ports.comports())
        for p in candidates:
            desc = (p.description or "").lower()
            hwid = (p.hwid or "").lower()
            if any(x in desc for x in ["arduino", "ch340", "usb serial", "ttyacm", "ttyusb"]) or \
               any(x in hwid for x in ["2341:", "1a86:", "2a03:", "0403:"]):
                return p.device
        # Fallback: first ttyACM/ttyUSB
        for p in candidates:
            if "/ttyACM" in p.device or "/ttyUSB" in p.device:
                return p.device
        return None

    def start(self) -> None:
        if not serial:
            print("[WARN] pyserial not installed; running in simulation mode.")
            return
        try:
            self.port = self._detect_port()
            if not self.port:
                print("[WARN] Arduino serial port not found; server will simulate readings.")
                return
            self.ser = serial.Serial(self.port, self.baudrate, timeout=1)
            # Give the board time to reset after opening serial
            time.sleep(2.0)
            self.stop_event.clear()
            self.read_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.read_thread.start()
            print(f"[INFO] Connected to Arduino on {self.port}")
        except Exception as exc:
            print(f"[WARN] Could not open serial port: {exc}; running in simulation mode.")
            self.ser = None

    def stop(self) -> None:
        self.stop_event.set()
        if self.read_thread and self.read_thread.is_alive():
            self.read_thread.join(timeout=2)
        if self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass

    def _reader_loop(self) -> None:
        assert self.ser is not None
        buffer = b""
        while not self.stop_event.is_set():
            try:
                line = self.ser.readline()
                if not line:
                    continue
                buffer += line
                if buffer.endswith(b"\n"):
                    text = buffer.decode(errors="ignore").strip()
                    buffer = b""
                    self._parse_line(text)
            except Exception:
                time.sleep(0.1)

    def _parse_line(self, text: str) -> None:
        # Expected like: "LDR: 512 | Temperatura: 24.5 °C | Umidade: 60.0 %"
        try:
            ldr_match = re.search(r"LDR:\s*(\d+)", text)
            temp_match = re.search(r"Temperatura:\s*([-+]?\d+(?:\.\d+)?)", text)
            hum_match = re.search(r"Umidade:\s*([-+]?\d+(?:\.\d+)?)", text)
            with self.lock:
                if ldr_match:
                    self.last_ldr_value = int(ldr_match.group(1))
                if temp_match:
                    self.last_temperature_c = float(temp_match.group(1))
                if hum_match:
                    self.last_humidity_percent = float(hum_match.group(1))
        except Exception:
            pass

    def _write_command(self, cmd: str) -> None:
        if not self.ser or not self.ser.is_open:
            return
        payload = (cmd.strip() + "\n").encode()
        try:
            self.ser.write(payload)
        except Exception:
            pass

    def set_intensity(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        self.current_intensity_percent = percent
        self._write_command(f"SET_INTENSITY:{percent}")

    def set_mode(self, mode: str) -> None:
        canonical = mode.capitalize()
        if canonical not in {"Fria", "Quente", "Dinâmica"}:
            canonical = "Fria"
        self.current_mode = canonical
        # Also instruct device; map accented to ASCII
        ascii_mode = {"Fria": "FRIA", "Quente": "QUENTE", "Dinâmica": "DINAMICA"}[canonical]
        self._write_command(f"SET_MODE:{ascii_mode}")

    def set_visual_alert(self, active: bool) -> None:
        self.current_visual_alert = bool(active)
        self._write_command(f"SET_ALERT:{'ON' if active else 'OFF'}")

    def snapshot(self) -> SensorSnapshot:
        # Simulate noise if not provided by device
        simulated_noise = int(30 + (time.time() * 1000) % 40)
        with self.lock:
            temp = self.last_temperature_c
            hum = self.last_humidity_percent
        return SensorSnapshot(
            light_percent=self.current_intensity_percent,
            temperature_c=temp,
            humidity_percent=hum,
            noise_db=simulated_noise,
            mode=self.current_mode,
            visual_alert=self.current_visual_alert,
        )


app = FastAPI(title="Ambiente Vivo API")

# Allow cross-origin requests so the UI can be opened from file:// or other hosts
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

serial_mgr = SerialManager()
serial_mgr.start()

# Serve the existing index.html at root
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(ROOT_DIR, "index.html")

if os.path.exists(INDEX_PATH):
    @app.get("/")
    def serve_index():
        return FileResponse(INDEX_PATH)
else:
    # If index does not exist where server runs, mount a dummy static
    pass


@app.get("/api/sensors", response_model=SensorSnapshot)
def get_sensors() -> SensorSnapshot:
    return serial_mgr.snapshot()


@app.post("/api/set_intensity")
def api_set_intensity(req: ControlRequest):
    if req.value is None:
        raise HTTPException(status_code=400, detail="Missing 'value'")
    if not (0 <= int(req.value) <= 100):
        raise HTTPException(status_code=400, detail="value must be 0..100")
    serial_mgr.set_intensity(int(req.value))
    return {"ok": True}


@app.post("/api/set_mode")
def api_set_mode(req: ControlRequest):
    if not req.mode:
        raise HTTPException(status_code=400, detail="Missing 'mode'")
    serial_mgr.set_mode(req.mode)
    # Optionally sync a default intensity for modes
    if req.mode.capitalize() == "Fria":
        serial_mgr.set_intensity(100)
    elif req.mode.capitalize() == "Quente":
        serial_mgr.set_intensity(30)
    elif req.mode.capitalize() == "Dinâmica":
        serial_mgr.set_intensity(70)
    return {"ok": True}


@app.post("/api/set_visual_alert")
def api_set_alert(req: ControlRequest):
    if req.visual_alert is None:
        raise HTTPException(status_code=400, detail="Missing 'visual_alert'")
    serial_mgr.set_visual_alert(bool(req.visual_alert))
    return {"ok": True}


@app.on_event("shutdown")
def on_shutdown():
    serial_mgr.stop()
