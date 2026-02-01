"""rmcanview.py

Support for RM/Proemion "CANview" gateways (e.g. CANview USB) using the
Proemion *Byte Command Protocol* over a serial / virtual COM port.

This adapter family does **not** present itself as a SocketCAN network
interface on Linux. Instead, it exposes a USB-serial device (typically via an
FTDI converter) and uses a framed binary protocol.

Protocol reference:
  - "Proemion Byte Command Protocol" (Byte Command Manual)

Only the subset needed by PiPAT is implemented:
  - Receive CAN data frames (11-bit and 29-bit IDs)
  - Transmit CAN data frames (11-bit and 29-bit IDs)
  - Optional basic setup (set CAN bitrate; force active mode)
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import can
import serial


SOF = 0x43  # 'C'
EOF = 0x0D  # CR


def _xor_checksum(payload: bytes) -> int:
    """XOR checksum over all bytes in *payload* (returns 0-255)."""
    c = 0
    for b in payload:
        c ^= b
    return c & 0xFF


def build_cmd(cmd: int, data: bytes = b"") -> bytes:
    """Build a single Byte-Command frame.

    Frame layout (Byte Command Manual):
      SOF (0x43), LEN, CMD, DATA..., CHKSUM, EOF (0x0D)

    LEN includes the CMD byte + DATA length.
    CHKSUM is XOR of SOF, LEN, CMD and DATA bytes.
    """
    cmd &= 0xFF
    length = (1 + len(data)) & 0xFF
    frame = bytearray()
    frame.append(SOF)
    frame.append(length)
    frame.append(cmd)
    frame.extend(data)
    frame.append(_xor_checksum(frame))
    frame.append(EOF)
    return bytes(frame)


@dataclass(frozen=True)
class _DecodedCmd:
    cmd: int
    data: bytes


class _ByteCmdParser:
    """Incremental parser for the byte-command framing."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[_DecodedCmd]:
        if chunk:
            self._buf.extend(chunk)

        out: list[_DecodedCmd] = []

        while True:
            # Find SOF
            sof_idx = self._buf.find(bytes([SOF]))
            if sof_idx < 0:
                self._buf.clear()
                return out

            if sof_idx:
                del self._buf[:sof_idx]

            # Need at least SOF + LEN + CMD
            if len(self._buf) < 3:
                return out

            length = int(self._buf[1])  # CMD+DATA length
            total_len = length + 4  # SOF + LEN + (CMD+DATA) + CHK + EOF
            if len(self._buf) < total_len:
                return out

            frame = bytes(self._buf[:total_len])
            del self._buf[:total_len]

            # Validate EOF
            if frame[-1] != EOF:
                # resync: keep scanning
                continue

            chk = frame[-2]
            calc = _xor_checksum(frame[:-2])
            if chk != calc:
                # resync
                continue

            cmd = frame[2]
            data = frame[3:-2]
            out.append(_DecodedCmd(cmd=cmd, data=data))


_CIA_BAUD_TO_CODE = {
    10_000: 0x00,
    20_000: 0x01,
    50_000: 0x02,
    100_000: 0xFE,
    125_000: 0x03,
    250_000: 0x04,
    500_000: 0x05,
    800_000: 0x06,
    1_000_000: 0x07,
}


class RmCanViewBus(can.BusABC):
    """python-can Bus implementation for CANview USB in "byte mode"."""

    def __init__(
        self,
        channel: str,
        *,
        serial_baud: int = 115200,
        can_bitrate: int = 250000,
        do_setup: bool = True,
        clear_errors_on_init: bool = True,
        log_fn: Callable[[str], None] | None = None,
        serial_timeout: float = 0.05,
    ) -> None:
        super().__init__(channel=channel)
        self.channel_info = f"rmcanview:{channel}"
        self._log = log_fn or (lambda _msg: None)

        self._ser = serial.Serial(
            port=channel,
            baudrate=int(serial_baud),
            timeout=float(serial_timeout),
            write_timeout=0.5,
        )
        try:
            # Best-effort: clear stale data
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        except Exception:
            pass

        self._tx_lock = threading.Lock()
        self._rx_q: "queue.Queue[can.Message]" = queue.Queue()
        self._parser = _ByteCmdParser()

        self._run = threading.Event()
        self._run.set()

        # Optional: clear any latched CAN error state on startup.
        # Byte Command Manual: CAN controller reset (0x58) resets the error status.
        if clear_errors_on_init:
            self._send_cmd_raw(0x58, b"")
            self._drain_quick(0.25)

        # Optional setup (CAN bitrate + active mode)
        if do_setup:
            self._try_setup(can_bitrate)

        self._rx_thread = threading.Thread(target=self._rx_worker, name="rmcanview-rx", daemon=True)
        self._rx_thread.start()

    # ---- Public BusABC API ----

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if self._is_shutdown:
            raise can.CanOperationError("Bus is shut down")

        # python-can may provide bytearray, list[int], etc.
        data_bytes = bytes(msg.data or b"")

        is_ext = bool(getattr(msg, "is_extended_id", False))
        is_rtr = bool(getattr(msg, "is_remote_frame", False))

        arb_id = int(msg.arbitration_id)
        if is_ext:
            cmd = 0x06 if is_rtr else 0x02
            id_bytes = (arb_id & 0x1FFFFFFF).to_bytes(4, "big")
        else:
            cmd = 0x04 if is_rtr else 0x00
            id_bytes = (arb_id & 0x7FF).to_bytes(2, "big")

        if is_rtr:
            dlc = int(getattr(msg, "dlc", 0))
            payload = id_bytes + bytes([dlc & 0x0F])
        else:
            if len(data_bytes) > 8:
                raise can.CanError("CAN data length > 8 not supported")
            payload = id_bytes + data_bytes

        frame = build_cmd(cmd, payload)

        with self._tx_lock:
            try:
                self._ser.write(frame)
                self._ser.flush()
            except Exception as e:
                raise can.CanOperationError(str(e)) from e

    def _recv_internal(self, timeout: Optional[float]) -> tuple[Optional[can.Message], bool]:
        if self._is_shutdown:
            return None, False

        try:
            if timeout is None:
                msg = self._rx_q.get(block=True)
            else:
                msg = self._rx_q.get(block=True, timeout=float(timeout))
            return msg, False
        except queue.Empty:
            return None, False

    def shutdown(self) -> None:
        if self._is_shutdown:
            return

        # Stop our RX thread first
        self._run.clear()
        try:
            if getattr(self, "_rx_thread", None):
                self._rx_thread.join(timeout=1.0)
        except Exception:
            pass

        try:
            if getattr(self, "_ser", None):
                self._ser.close()
        except Exception:
            pass

        super().shutdown()

    # ---- Internal helpers ----

    def _rx_worker(self) -> None:
        """Read the serial stream and convert CAN frames into python-can Messages."""
        while self._run.is_set() and not self._is_shutdown:
            try:
                chunk = self._ser.read(256)
            except Exception:
                # Serial device went away?
                break

            if not chunk:
                continue

            for dec in self._parser.feed(chunk):
                msg = self._decode_can_message(dec)
                if msg is not None:
                    # Do not block the reader thread; drop on extreme backpressure.
                    try:
                        self._rx_q.put_nowait(msg)
                    except Exception:
                        pass

    def _decode_can_message(self, dec: _DecodedCmd) -> Optional[can.Message]:
        """Translate Byte-Command process-data messages into can.Message.

        Returns None for non-CAN messages.
        """
        cmd = int(dec.cmd)
        data = dec.data

        # Data frames (received)
        if cmd in (0x00, 0x01):
            # 11-bit ID; cmd 0x01 includes a 32-bit timestamp
            if len(data) < 2:
                return None
            if cmd == 0x01:
                if len(data) < 2 + 4:
                    return None
                payload = data[2:-4]
            else:
                payload = data[2:]
            arb_id = int.from_bytes(data[:2], "big") & 0x7FF
            if len(payload) > 8:
                return None
            return can.Message(
                arbitration_id=arb_id,
                is_extended_id=False,
                data=payload,
                is_remote_frame=False,
                timestamp=time.time(),
            )

        if cmd in (0x02, 0x03):
            # 29-bit ID; cmd 0x03 includes a 32-bit timestamp
            if len(data) < 4:
                return None
            if cmd == 0x03:
                if len(data) < 4 + 4:
                    return None
                payload = data[4:-4]
            else:
                payload = data[4:]
            arb_id = int.from_bytes(data[:4], "big") & 0x1FFFFFFF
            if len(payload) > 8:
                return None
            return can.Message(
                arbitration_id=arb_id,
                is_extended_id=True,
                data=payload,
                is_remote_frame=False,
                timestamp=time.time(),
            )

        # Remote frames (received)
        if cmd in (0x04, 0x05):
            if len(data) < 2 + 1:
                return None
            dlc = int(data[2]) & 0x0F
            arb_id = int.from_bytes(data[:2], "big") & 0x7FF
            return can.Message(
                arbitration_id=arb_id,
                is_extended_id=False,
                is_remote_frame=True,
                dlc=dlc,
                data=b"",
                timestamp=time.time(),
            )

        if cmd in (0x06, 0x07):
            if len(data) < 4 + 1:
                return None
            dlc = int(data[4]) & 0x0F
            arb_id = int.from_bytes(data[:4], "big") & 0x1FFFFFFF
            return can.Message(
                arbitration_id=arb_id,
                is_extended_id=True,
                is_remote_frame=True,
                dlc=dlc,
                data=b"",
                timestamp=time.time(),
            )

        # Everything else (feedback, config replies, diagnostic data) is ignored.
        return None

    def _try_setup(self, can_bitrate: int) -> None:
        """Best-effort: set CAN bitrate and force active mode."""
        # 0x57 = set CAN baud rate parameters
        code = _CIA_BAUD_TO_CODE.get(int(can_bitrate))
        if code is None:
            self._log(f"RMCAN: unsupported CAN bitrate {can_bitrate}; skipping adapter bitrate setup")
        else:
            # Data bytes: [code, BTR0, BTR1, BTR2, BTR3]
            self._send_cmd_raw(0x57, bytes([code, 0, 0, 0, 0]))
            # Reply is ignored; flush any immediate response
            self._drain_quick(0.25)

        # 0x5B = set active/passive mode; 0x00 = active
        self._send_cmd_raw(0x5B, b"\x00")
        self._drain_quick(0.25)

    def _send_cmd_raw(self, cmd: int, data: bytes) -> None:
        frame = build_cmd(cmd, data)
        with self._tx_lock:
            try:
                self._ser.write(frame)
                self._ser.flush()
            except Exception as e:
                self._log(f"RMCAN: failed to send cmd 0x{cmd:02X}: {e}")

    def _drain_quick(self, seconds: float) -> None:
        """Drain and ignore whatever the device replies for a short period."""
        deadline = time.time() + float(seconds)
        while time.time() < deadline:
            try:
                chunk = self._ser.read(256)
            except Exception:
                return
            if not chunk:
                continue
            # Feed parser to stay in sync, but discard results.
            self._parser.feed(chunk)
