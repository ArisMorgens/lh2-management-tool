#!/usr/bin/env python3
"""
USB serial console of a Valve v2 lighthouse, used to read and set its channel.

Same command sequence as the "LH Basestation Setup" dialog in cfclient
(crazyflie-clients-python, ui/dialogs/basestation_mode_dialog.py).
"""

import re
import time

import serial
from serial.tools.list_ports import comports

VID = 0x28de
PID = 0x2500
READ_TIMEOUT = 0.4
WRITE_TIMEOUT = 2
# cap on one response, so a console that never goes quiet can't hang us
RESPONSE_TIMEOUT = 5
CHANNEL_MIN = 1
CHANNEL_MAX = 16


def find_ports():
    """Serial device paths of every base station connected over USB."""
    return [p.device for p in comports() if p.vid == VID and p.pid == PID]


class ConsoleNotResponding(Exception):
    """The USB port is there but the console doesn't accept input (seen after the console's 'reboot')."""


class SaveFailed(Exception):
    """'param save' didn't confirm the write, so the channel would be lost at power-off."""


class LhUsb:
    def __init__(self, port):
        self.port = port

    def _command(self, ser, text):
        """Send one console command and return the response lines (until the port goes quiet)."""
        try:
            ser.write(f'\r\n{text}\r\n'.encode())
            ser.flush()
        except serial.SerialTimeoutException as e:
            # drop the unsent bytes, or closing the port blocks while the tty tries to drain them
            ser.reset_output_buffer()
            raise ConsoleNotResponding(f'{self.port}: console does not accept input') from e
        data = b''
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while time.monotonic() < deadline:
            chunk = ser.read(4096)
            if not chunk:
                break  # quiet for READ_TIMEOUT: response complete
            data += chunk
        return [line.rstrip('\r') for line in data.decode(errors='replace').split('\n') if line.strip()]

    def _open(self):
        try:
            return serial.Serial(self.port, timeout=READ_TIMEOUT, write_timeout=WRITE_TIMEOUT)
        except serial.SerialException as e:
            raise PermissionError(
                f'Cannot open {self.port} ({e}). If this is a permission problem, run '
                '"sudo usermod -aG dialout $USER" and log in again.') from e

    @staticmethod
    def _parse_mode(lines):
        for line in lines:
            if line.startswith('Current mode: '):
                return int(line.split()[2])
        return None

    @staticmethod
    def _parse_fields(lines):
        """'Key: value' lines of a response (e.g. 'Serial Number: 396F1AEE') as a dict.

        The separator can be a tab instead of a space ('Radio Build:\\tFW 2.2...' on 2.x units).
        """
        fields = {}
        for line in lines:
            m = re.match(r'([A-Za-z][A-Za-z ]*):\s+(.*)', line)
            if m and not line.startswith('lhtx>'):
                fields[m.group(1).strip()] = m.group(2).strip()
        return fields

    def read_info(self):
        """Return {'channel', 'uid', 'fields', 'mode_lines', 'id_lines'}; channel 0 means not supported.

        'uid' is the station's ID (printed as 'Serial Number' by the console),
        which is also the suffix of its BLE name (ID 396F1AEE advertises as
        LHB-396F1AEE).
        """
        with self._open() as ser:
            mode_lines = self._command(ser, 'mode')
            id_lines = self._command(ser, 'id')
        fields = self._parse_fields(id_lines)
        return {'channel': self._parse_mode(mode_lines), 'uid': fields.get('Serial Number'),
                'fields': fields, 'mode_lines': mode_lines, 'id_lines': id_lines}

    def set_channel(self, channel):
        """Set and persist the channel; returns the channel the station reports afterwards.

        Unlike cfclient, the 'param save' reply is checked: the final 'mode'
        read only shows the live setting, not whether it was stored.
        """
        if not CHANNEL_MIN <= channel <= CHANNEL_MAX:
            raise ValueError(f'Channel must be {CHANNEL_MIN}-{CHANNEL_MAX}')
        with self._open() as ser:
            self._command(ser, f'mode {channel}')
            time.sleep(1)
            save_lines = self._command(ser, 'param save')
            # e.g. "374 bytes written to partition 1."
            if not any('bytes written to partition' in line for line in save_lines):
                raise SaveFailed(f'Channel {channel} set but not saved; station replied: '
                                 + ' | '.join(save_lines))
            time.sleep(1)
            return self._parse_mode(self._command(ser, 'mode'))
