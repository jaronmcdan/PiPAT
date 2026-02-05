"""RM CANview (Proemion) USB/serial CAN interface.

This implements the Proemion *Byte Command Protocol* framing used by several
RM/Proemion CAN gateways (including CANview USB) when operated in *Byte Mode*.

It is intentionally minimal: PiPAT only needs the ability to send and receive
raw CAN data frames.

Protocol framing (Byte Mode):
  SOF   : 0x43 ('C')
  LEN   : number of bytes in (CMD + DATA)
  CMD   : 1 byte
  DATA  : variable
  CHK   : XOR of SOF, LEN, CMD, and DATA bytes
  EOF   : 0x0D ('\r')

Relevant commands:
  0x00/0x01 : 11-bit CAN data frame received (0x01 includes timestamp)
  0x02/0x03 : 29-bit CAN data frame received (0x03 includes timestamp)
  0x00      : transmit 11-bit CAN data frame
  0x02      : transmit 29-bit CAN data frame
  0x61      : set feedback/output settings (bit0 enables CAN output)
  0x57      : set CAN baud rate

See: "Proemion Byte Command Protocol – Binary commands".
"""

from __future__ import annotations

import glob
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


try:
    import serial  # type: ignore
    from serial.tools import list_ports  # type: ignore
except Exception:  # pragma: no cover
    serial = None
    list_ports = None


try:
    import can  # type: ignore
except Exception:  # pragma: no cover
    can = None


SOF = 0x43
EOF = 0x0D


def _resolve_by_id(dev: str) -> str:
    """Prefer /dev/serial/by-id/... when available (stable across reboots)."""

    try:
        real = os.path.realpath(dev)
    except Exception:
        return dev

    by_id_dir = "/dev/serial/by-id"
    if not os.path.isdir(by_id_dir):
        return dev

    for p in glob.glob(os.path.join(by_id_dir, "*")):
        try:
            if os.path.realpath(p) == real:
                return p
        except Exception:
            continue
    return dev


def find_rmcanview_port(*, vid: int = 0x0403, pid: int = 0xFD60) -> Optional[str]:
    """Return a serial device path for RM CANview USB if present.

    Detection strategy:
      1) Match USB VID/PID reported by pyserial.
      2) Fallback to matching the VID:PID substring in the HWID string.
      3) As a last resort, look for "canview" in the description.
    """

    if list_ports is None:
        return None

    want = f"{vid:04x}:{pid:04x}".lower()
    best: Optional[str] = None

    for p in list_ports.comports():
        try:
            p_vid = getattr(p, "vid", None)
            p_pid = getattr(p, "pid", None)

            if (p_vid is not None) and (p_pid is not None):
                if int(p_vid) == int(vid) and int(p_pid) == int(pid):
                    return _resolve_by_id(p.device)

            hwid = str(getattr(p, "hwid", "") or "").lower()
            desc = str(getattr(p, "description", "") or "").lower()

            if want in hwid:
                return _resolve_by_id(p.device)

            if (best is None) and ("canview" in desc or "rmcan" in desc):
                best = p.device
        except Exception:
            continue

    return _resolve_by_id(best) if best else None


def _xor_checksum(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c & 0xFF


def _build_cmd(cmd: int, payload: bytes = b"") -> bytes:
    """Build a Byte Mode command frame."""

    cmd_b = bytes([cmd & 0xFF])
    length = (len(cmd_b) + len(payload)) & 0xFF
    header = bytes([SOF, length]) + cmd_b + payload
    chk = _xor_checksum(header)
    return header + bytes([chk, EOF])


_BAUD_CODE = {
    10_000: 0x00,
    20_000: 0x01,
    50_000: 0x02,
    100_000: 0xFE,  # yes, this is what the protocol manual specifies
    125_000: 0x03,
    250_000: 0x04,
    500_000: 0x05,
    800_000: 0x06,
    1_000_000: 0x07,
}


@dataclass
class _ParsedFrame:
    cmd: int
    payload: bytes


class RmCanviewBus:
    """A minimal CAN bus adapter for RM CANview using pyserial."""

    def __init__(
        self,
        *,
        port: str,
        tty_baudrate: int = 115200,
        can_bitrate: int = 250_000,
        set_can_baud: bool = True,
        enable_can_output: bool = True,
        log: Optional[Callable[[str], None]] = None,
        read_chunk: int = 256,
        read_timeout_s: float = 0.05,
    ) -> None:

        if serial is None:
            raise RuntimeError("pyserial is not installed")
        if can is None:
            raise RuntimeError("python-can is not installed")

        self.port = port
        self.tty_baudrate = int(tty_baudrate)
        self.can_bitrate = int(can_bitrate)
        self.channel_info = f"rmcanview:{port}"  # python-can style
        self._log = log or (lambda _s: None)
        self._read_chunk = int(read_chunk)

        self._tx_lock = threading.Lock()
        self._rx_q: "queue.Queue[can.Message]" = queue.Queue()

        self._stop = threading.Event()
        self._rx_buf = bytearray()

        # Open serial
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.tty_baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=float(read_timeout_s),
            write_timeout=1.0,
        )
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass

        # Start reader thread
        self._thread = threading.Thread(target=self._reader_loop, name="rmcanview-rx", daemon=True)
        self._thread.start()

        # Configure device
        if set_can_baud:
            self._set_can_baudrate(self.can_bitrate)

        if enable_can_output:
            # Enable CAN output, but do NOT enable TX feedback (keeps RX clean)
            # bit0: CAN output, bit3: RS232 interface on
            flags = 0x09
            self._write_cmd(0x61, bytes([flags]))

        self._log(f"RM CANview ready on {self.port} (tty_baud={self.tty_baudrate}, can_baud={self.can_bitrate})")

    # --- Public bus API (subset of python-can) ---

    def shutdown(self) -> None:
        self._stop.set()
        try:
            if self._thread.is_alive():
                self._thread.join(timeout=1.0)
        except Exception:
            pass

        try:
            self._ser.close()
        except Exception:
            pass

    def recv(self, timeout: Optional[float] = None):
        """Receive a CAN message.

        Returns a python-can Message instance or None.
        """

        try:
            if timeout is None:
                return self._rx_q.get()
            return self._rx_q.get(timeout=float(timeout))
        except queue.Empty:
            return None

    def send(self, msg, timeout: Optional[float] = None) -> None:
        """Send a CAN message (data frame)."""

        # Accept either python-can Message or a duck-typed object.
        arb_id = int(getattr(msg, "arbitration_id"))
        is_ext = bool(getattr(msg, "is_extended_id", arb_id > 0x7FF))
        data = getattr(msg, "data", b"")
        data_b = bytes(data) if data is not None else b""
        if len(data_b) > 8:
            raise ValueError("CAN payload > 8 bytes not supported")

        if is_ext:
            cmd = 0x02
            payload = int(arb_id & 0x1FFFFFFF).to_bytes(4, "big") + data_b
        else:
            cmd = 0x00
            payload = int(arb_id & 0x7FF).to_bytes(2, "big") + data_b

        frame = _build_cmd(cmd, payload)
        with self._tx_lock:
            self._ser.write(frame)
            try:
                self._ser.flush()
            except Exception:
                pass

    # --- Internal helpers ---

    def _write_cmd(self, cmd: int, payload: bytes = b"") -> None:
        frame = _build_cmd(cmd, payload)
        with self._tx_lock:
            self._ser.write(frame)
            try:
                self._ser.flush()
            except Exception:
                pass

    def _set_can_baudrate(self, bitrate: int) -> None:
        code = _BAUD_CODE.get(int(bitrate))
        if code is None:
            raise ValueError(
                f"Unsupported CAN bitrate {bitrate}. Supported: {', '.join(str(k) for k in sorted(_BAUD_CODE))}"
            )

        # 0x57 = Set CAN baud rate parameters
        self._write_cmd(0x57, bytes([code]))
        # Give the device a moment to apply the setting.
        time.sleep(0.05)

    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(self._read_chunk)
            except Exception:
                time.sleep(0.05)
                continue

            if chunk:
                self._rx_buf.extend(chunk)

            # Parse as many frames as we can.
            while True:
                frm = self._try_parse_one()
                if frm is None:
                    break
                self._handle_frame(frm)

    def _try_parse_one(self) -> Optional[_ParsedFrame]:
        buf = self._rx_buf

        # We keep resyncing by searching for SOF.
        while True:
            if not buf:
                return None

            sof_i = buf.find(bytes([SOF]))
            if sof_i < 0:
                buf.clear()
                return None
            if sof_i > 0:
                del buf[:sof_i]

            # Need at least SOF + LEN + CMD + CHK + EOF (min length=1)
            if len(buf) < 5:
                return None

            length = int(buf[1])
            total = length + 4
            if total < 5 or total > 300:
                # Bogus length; drop SOF and resync
                del buf[0]
                continue

            if len(buf) < total:
                return None

            frame = bytes(buf[:total])
            if frame[-1] != EOF:
                del buf[0]
                continue

            chk = frame[-2]
            calc = _xor_checksum(frame[:-2])
            if chk != calc:
                del buf[0]
                continue

            # Valid frame: consume it
            del buf[:total]
            cmd = frame[2]
            payload = frame[3:-2]
            return _ParsedFrame(cmd=cmd, payload=payload)

    def _handle_frame(self, frm: _ParsedFrame) -> None:
        cmd = int(frm.cmd) & 0xFF
        p = frm.payload

        # CAN data frames received from the bus
        if cmd in (0x00, 0x01):
            with_ts = cmd == 0x01
            ts_len = 4 if with_ts else 0
            if len(p) < (2 + ts_len):
                return
            can_id = ((p[0] << 8) | p[1]) & 0x7FF
            data_end = len(p) - ts_len
            data = p[2:data_end]
            if len(data) > 8:
                return
            try:
                msg = can.Message(arbitration_id=int(can_id), data=bytearray(data), is_extended_id=False)
                msg.timestamp = time.time()
                self._rx_q.put(msg)
            except Exception:
                return
            return

        if cmd in (0x02, 0x03):
            with_ts = cmd == 0x03
            ts_len = 4 if with_ts else 0
            if len(p) < (4 + ts_len):
                return
            can_id = int.from_bytes(p[0:4], "big") & 0x1FFFFFFF
            data_end = len(p) - ts_len
            data = p[4:data_end]
            if len(data) > 8:
                return
            try:
                msg = can.Message(arbitration_id=int(can_id), data=bytearray(data), is_extended_id=True)
                msg.timestamp = time.time()
                self._rx_q.put(msg)
            except Exception:
                return
            return

        # Common replies we might see during init; log at low volume
        if cmd in (0x48,):
            self._log("RM CANview: device busy / command not supported")
            return

        # Ignore everything else (status, feedback, configuration replies, etc.)
        return
