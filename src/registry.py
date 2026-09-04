#!/usr/bin/env python3
"""
JSON-backed registry mapping station names to MAC addresses
and their mode value.
"""

import json
import os

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stations.json')


def load():
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, 'r') as f:
        return json.load(f)


def save(data):
    with open(STORE_PATH, 'w') as f:
        json.dump(data, f, indent=2, sort_keys=True)


def all():
    return load()


def add(name, mac, mode=None, firmware=None):
    data = load()
    data[name] = {'mac': mac, 'mode': mode, 'firmware': firmware}
    save(data)


def update(name, **fields):
    data = load()
    if name in data:
        data[name].update(fields)
        save(data)


def remove(name):
    data = load()
    if name in data:
        del data[name]
        save(data)
