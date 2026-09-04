#!/usr/bin/env python3
"""
Dump all BT LE GATT services and characteristics of a Valve v2 lighthouse.
"""

from bluepy import btle

import time
from argparse import ArgumentParser

TRY_COUNT = 5
TRY_PAUSE = 2


def connect(mac, iface, try_count, try_pause):
    dev = btle.Peripheral()
    while True:
        try:
            print(f'Connecting to {mac} ... ', end='', flush=True)
            dev.connect(mac, iface=iface, addrType=btle.ADDR_TYPE_RANDOM)
            print('connected')
            return dev
        except btle.BTLEDisconnectError as e:
            if try_count <= 1:
                raise e
            print(f'failed ({e}), retrying...')
            try_count -= 1
            time.sleep(try_pause)


def describe_props(charc):
    props = charc.propertiesToString().strip()
    return props


def dump(dev):
    for svc in dev.getServices():
        print(f'\nService {svc.uuid} ({svc.uuid.getCommonName()})')
        try:
            chars = svc.getCharacteristics()
        except btle.BTLEException as e:
            print(f'  <failed to get characteristics: {e}>')
            continue
        for c in chars:
            props = describe_props(c)
            line = f'  Characteristic {c.uuid} ({c.uuid.getCommonName()}) handle={c.getHandle():#06x} props={props}'
            value_str = ''
            if 'READ' in props:
                try:
                    val = c.read()
                    hex_str = val.hex()
                    try:
                        ascii_str = val.decode('ascii')
                        if ascii_str.isprintable():
                            value_str = f'hex={hex_str} ascii={ascii_str!r}'
                        else:
                            value_str = f'hex={hex_str}'
                    except UnicodeDecodeError:
                        value_str = f'hex={hex_str}'
                except btle.BTLEException as e:
                    value_str = f'<read failed: {e}>'
            print(line)
            if value_str:
                print(f'    value: {value_str}')


def main():
    ap = ArgumentParser(description='Dump all GATT services/characteristics of a Valve v2 lighthouse')
    ap.add_argument('lh_mac', type=str, help='MAC address of the lighthouse (aa:bb:cc:dd:ee:ff)')
    ap.add_argument('-i', '--interface', type=int, default=0, help='Bluetooth interface (0=hci0, 1=hci1, ...) [%(default)s]')
    ap.add_argument('--try_count', type=int, default=TRY_COUNT, help='number of connection attempts [%(default)s]')
    ap.add_argument('--try_pause', type=int, default=TRY_PAUSE, help='sleep time between reconnect attempts [%(default)s]')
    args = ap.parse_args()

    dev = connect(args.lh_mac, args.interface, args.try_count, args.try_pause)
    try:
        dump(dev)
    finally:
        dev.disconnect()


if __name__ == '__main__':
    main()
