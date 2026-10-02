#!/usr/bin/env python3
"""
JSON-backed registry mapping station names to MAC addresses.
Nothing else is stored; everything else is read live from the stations.
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


def add(name, mac):
    data = load()
    data[name] = {'mac': mac}
    save(data)


def rename(old, new):
    data = load()
    if old in data:
        data[new] = data.pop(old)
        save(data)


def remove(name):
    data = load()
    if name in data:
        del data[name]
        save(data)
