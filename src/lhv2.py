#!/usr/bin/env python3
"""
Shared BT LE GATT abstraction for a Valve v2 lighthouse.
"""

from bluepy import btle

import time

# verbosity level INFO
INFO = 1

# LHv2 GATT service
LHV2_GATT_SERVICE_UUID = btle.UUID('00001523-1212-efde-1523-785feabcd124')
LHV2_GATT_CHAR_POWER_CTRL_UUID = btle.UUID('00001525-1212-efde-1523-785feabcd124')
LHV2_GATT_CHAR_MODE_UUID = btle.UUID('00001524-1212-efde-1523-785feabcd124')
LHV2_GATT_CHAR_IDENTIFY_UUID = btle.UUID('00008421-1212-efde-1523-785feabcd124')
# Power management
POWER_ON = b'\x01'
POWER_OFF = b'\x00'

# Documented read-back values for LHV2_GATT_CHAR_POWER_CTRL_UUID:
# https://gist.github.com/BenWoodford/3a1e500a4ea2673525f5adb4120fd47c
# 0x01/0x09/0x0b all mean awake, differing only in which state preceded it
# (from standby vs. from sleep) -- this tool has no standby command, so that
# distinction isn't surfaced, just "Awake".
POWER_STATE_LABELS = {
    0x00: 'Sleeping',
    0x01: 'Awake',
    0x02: 'Standby',
    0x09: 'Awake',
    0x0b: 'Awake',
}


class IdentifyNotSupported(Exception):
    """Raised when a station's firmware doesn't expose the Identify characteristic."""


def decodePowerState(raw):
    """Decode a power-control characteristic read-back into a human label."""
    if not raw:
        return 'Unknown'
    return POWER_STATE_LABELS.get(raw[0], f'Unknown (0x{raw[0]:02x})')


class LHV2:
    """LHv2 abstraction."""
    def __init__(self, macAddr, hciIface, verbose=0):
        """Connect to the BTLE server in LHv2."""
        self.dev = btle.Peripheral()
        self.macAddr = macAddr
        self.hciIface = hciIface
        self.verbose = verbose
        self.characteristics = None
        self.name = None

    def connect(self, try_count, try_pause):
        """Connect to LH, try it `try_count` times."""
        while True:
            try:
                if (self.verbose >= INFO):
                    print(f'Connecting to {self.macAddr} at {time.asctime()} -> ', end='')
                self.dev.connect(self.macAddr, iface=self.hciIface, addrType=btle.ADDR_TYPE_RANDOM)
                if (self.verbose >= INFO):
                    print(self.dev.getState())
                break
            except btle.BTLEDisconnectError as e:
                if try_count <= 1:
                    raise e
                if (self.verbose >= INFO):
                    print(e)
                try_count -= 1
                time.sleep(try_pause)
                continue
            except:
                raise
        # only discover characteristics on the first connect
        if self.characteristics is None:
            chars = self.dev.getCharacteristics()
            self.characteristics = dict([(c.uuid, c) for c in chars])
        if self.name is None:
            self.name = self.getCharacteristic(btle.AssignedNumbers.device_name).read().decode()
        if self.verbose >= INFO:
            mode = self.getCharacteristic(LHV2_GATT_CHAR_MODE_UUID).read()
            print(f'Connected to {self.name} ({self.dev.addr}, mode={mode.hex()})')

    def disconnect(self):
        if self.verbose >= INFO:
            print(f'Diconnecting from {self.name} at {time.asctime()}')
        self.dev.disconnect()

    def getCharacteristic(self, uuid):
        return self.characteristics[uuid]

    def writeCharacteristic(self, uuid, val):
        charc = self.getCharacteristic(uuid)
        charc.write(val, withResponse=True)
        if self.verbose >= INFO:
            print(f'Writing {val.hex()} to {charc.uuid.getCommonName()}')

    def readMode(self):
        return self.getCharacteristic(LHV2_GATT_CHAR_MODE_UUID).read()

    def readPowerState(self):
        return self.getCharacteristic(LHV2_GATT_CHAR_POWER_CTRL_UUID).read()

    def readFirmwareRevision(self):
        raw = self.getCharacteristic(btle.AssignedNumbers.firmware_revision_string).read()
        text = raw.decode(errors='replace').strip()
        # some firmware separates the R/M/B fields with newlines instead of
        # commas, which a single-line table cell would otherwise swallow
        return ', '.join(line.strip() for line in text.splitlines() if line.strip())

    def getName(self):
        return self.name

    def identify(self):
        """Flash the station's LED so it can be spotted physically."""
        if LHV2_GATT_CHAR_IDENTIFY_UUID not in self.characteristics:
            raise IdentifyNotSupported('Identify not supported on this firmware')
        self.writeCharacteristic(LHV2_GATT_CHAR_IDENTIFY_UUID, b'\x01')

    def powerOn(self):
        self.writeCharacteristic(LHV2_GATT_CHAR_POWER_CTRL_UUID, POWER_ON)

    def powerOff(self):
        # Documented sleep sequence is wake (0x01) then sleep (0x00) as two
        # separate writes, not a single 0x00 write:
        # https://gist.github.com/BenWoodford/3a1e500a4ea2673525f5adb4120fd47c
        self.writeCharacteristic(LHV2_GATT_CHAR_POWER_CTRL_UUID, POWER_ON)
        self.writeCharacteristic(LHV2_GATT_CHAR_POWER_CTRL_UUID, POWER_OFF)
