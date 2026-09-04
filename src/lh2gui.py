#!/usr/bin/env python3
"""
Simple desktop GUI for managing Valve v2 lighthouse base stations.
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from bluepy import btle

import registry
from lhv2 import LHV2, decodePowerState, IdentifyNotSupported

SCAN_TIMEOUT = 8
SCAN_TICK_MS = 100
INTERFACE = 0
TRY_COUNT = 3
TRY_PAUSE = 2

COLUMNS = ('name', 'mode', 'mac', 'firmware', 'status')
COLUMN_LABELS = ('Name', 'Mode', 'MAC', 'Firmware', 'Status')
SEPARATOR_IID = '__separator__'


class Lh2Gui:
    def __init__(self, root):
        self.root = root
        self.root.title('Lighthouse Base Station Manager')
        self.events = queue.Queue()
        self.stations = {}   # mac -> {'name','mode','mac','firmware','status'}; name=None means unregistered
        self.busy_macs = set()
        self._scan_active = False

        self._build_ui()
        self._load_registered()
        self._refresh_all_statuses()
        self.root.after(100, self._poll_events)

    def _build_ui(self):
        main_frame = ttk.LabelFrame(self.root, text='Base stations')
        main_frame.pack(fill='both', expand=True, padx=8, pady=8)

        self.tree = ttk.Treeview(main_frame, columns=COLUMNS, show='headings',
                                  height=14, selectmode='extended')
        for col, label in zip(COLUMNS, COLUMN_LABELS):
            self.tree.heading(col, text=label)
        self.tree.pack(fill='both', expand=True, side='left')

        btns = ttk.Frame(main_frame)
        btns.pack(side='left', fill='y', padx=8)
        self.scan_btn = ttk.Button(btns, text='Scan', command=self._start_scan)
        self.scan_btn.pack(fill='x', pady=2)
        ttk.Separator(btns).pack(fill='x', pady=6)
        ttk.Button(btns, text='On', command=lambda: self._power_selected(True)).pack(fill='x', pady=2)
        ttk.Button(btns, text='Off', command=lambda: self._power_selected(False)).pack(fill='x', pady=2)
        ttk.Button(btns, text='Get Status', command=self._get_status_selected).pack(fill='x', pady=2)
        ttk.Button(btns, text='Identify', command=self._identify_selected).pack(fill='x', pady=2)
        ttk.Separator(btns).pack(fill='x', pady=6)
        ttk.Button(btns, text='Register', command=self._register_selected).pack(fill='x', pady=2)
        ttk.Button(btns, text='Rename', command=self._rename_selected).pack(fill='x', pady=2)
        ttk.Button(btns, text='Remove', command=self._remove_selected).pack(fill='x', pady=2)

        self.scan_progress = ttk.Progressbar(self.root, mode='determinate', maximum=100)
        self.scan_progress.pack(fill='x', padx=8)

        self.status_var = tk.StringVar(value='Ready')
        ttk.Label(self.root, textvariable=self.status_var, anchor='w').pack(fill='x', padx=8, pady=(0, 8))

    # ---- table state ----------------------------------------------------------
    def _load_registered(self):
        for name, info in registry.all().items():
            mac = info['mac']
            st = self.stations.setdefault(mac, self._blank_station(mac))
            st['name'] = name
            st['mode'] = info.get('mode')
            st['firmware'] = info.get('firmware')
        self._refresh_table()

    def _refresh_all_statuses(self):
        for mac, st in self.stations.items():
            if st['name']:
                self._get_status_one(mac)

    @staticmethod
    def _blank_station(mac):
        return {'name': None, 'mode': None, 'mac': mac, 'firmware': None, 'status': None}

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())

        registered = sorted(
            ((mac, st) for mac, st in self.stations.items() if st['name']),
            key=lambda item: item[1]['name'].lower())
        unregistered = sorted(
            ((mac, st) for mac, st in self.stations.items() if not st['name']),
            key=lambda item: item[0])

        for mac, st in registered:
            self._insert_row(mac, st)
        if registered and unregistered:
            sep = '─' * 14
            self.tree.insert('', 'end', iid=SEPARATOR_IID, values=(sep,) * len(COLUMNS))
        for mac, st in unregistered:
            self._insert_row(mac, st)

    def _insert_row(self, mac, st):
        self.tree.insert('', 'end', iid=mac, values=(
            st['name'] or '', st['mode'] if st['mode'] is not None else '',
            mac, st['firmware'] or '', st['status'] or ''))

    def _selected_macs(self):
        sel = [mac for mac in self.tree.selection() if mac != SEPARATOR_IID]
        if not sel:
            messagebox.showinfo('No selection', 'Select one or more stations first.')
        return sel

    def _set_cell(self, mac, col, text):
        if mac in self.stations:
            self.stations[mac][col] = text
        if self.tree.exists(mac):
            vals = list(self.tree.item(mac, 'values'))
            vals[COLUMNS.index(col)] = text
            self.tree.item(mac, values=vals)

    # ---- scan -------------------------------------------------------------------
    def _start_scan(self):
        self.scan_btn.config(state='disabled')
        self.status_var.set('Scanning...')
        self.scan_progress['value'] = 0
        self._scan_start = time.monotonic()
        self._scan_active = True
        self._tick_scan_progress()
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _tick_scan_progress(self):
        if not self._scan_active:
            return
        elapsed = time.monotonic() - self._scan_start
        pct = min(100, (elapsed / SCAN_TIMEOUT) * 100)
        self.scan_progress['value'] = pct
        if pct < 100:
            self.root.after(SCAN_TICK_MS, self._tick_scan_progress)

    def _scan_worker(self):
        try:
            scanner = btle.Scanner()
            devices = scanner.scan(SCAN_TIMEOUT)
            found = []
            for dev in devices:
                name = dev.getValueText(btle.ScanEntry.COMPLETE_LOCAL_NAME) or \
                    dev.getValueText(btle.ScanEntry.SHORT_LOCAL_NAME)
                if name and name.startswith('LHB-'):
                    found.append(dev.addr.upper())
            self.events.put(('scan_done', found))
        except Exception as e:
            self.events.put(('scan_error', str(e)))

    # ---- register ---------------------------------------------------------------
    def _register_selected(self):
        for mac in self._selected_macs():
            st = self.stations.get(mac)
            if st and st['name']:
                continue  # already registered
            self.status_var.set(f'Reading {mac}...')
            threading.Thread(target=self._register_worker, args=(mac,), daemon=True).start()

    def _register_worker(self, mac):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            mode_hex = lhv2.readMode().hex()
            firmware = lhv2.readFirmwareRevision()
            adv_name = lhv2.getName() or mac
            lhv2.disconnect()
        except Exception as e:
            self.events.put(('register_read_failed', (mac, str(e))))
            return
        self.events.put(('register_ready', (mac, adv_name, mode_hex, firmware)))

    def _open_register_dialog(self, mac, adv_name, mode_hex, firmware=None):
        name = simpledialog.askstring(
            'Register station', f'Name for {adv_name} ({mac}):', initialvalue=adv_name)
        if not name:
            return
        mode_value = int(mode_hex, 16) if mode_hex else None
        registry.add(name, mac, mode_value, firmware=firmware)
        st = self.stations.setdefault(mac, self._blank_station(mac))
        st['name'] = name
        st['mode'] = mode_value
        st['firmware'] = firmware
        self._refresh_table()
        self.status_var.set(f'Registered {name}')

    # ---- power control ------------------------------------------------------------
    def _power_selected(self, turn_on):
        for mac in self._selected_macs():
            self._power_one(mac, turn_on)

    def _power_one(self, mac, turn_on):
        if mac in self.busy_macs:
            return
        self.busy_macs.add(mac)
        self._set_cell(mac, 'status', 'On...' if turn_on else 'Off...')
        self.status_var.set(f'{"Turning on" if turn_on else "Turning off"} {mac}...')
        threading.Thread(target=self._power_worker, args=(mac, turn_on), daemon=True).start()

    def _power_worker(self, mac, turn_on):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            if turn_on:
                lhv2.powerOn()
            else:
                lhv2.powerOff()
            try:
                power_label = decodePowerState(lhv2.readPowerState())
            except Exception:
                power_label = None
            lhv2.disconnect()
            self.events.put(('power_done', (mac, turn_on, power_label)))
        except Exception as e:
            self.events.put(('power_failed', (mac, turn_on, str(e))))

    # ---- status query --------------------------------------------------------------
    def _get_status_selected(self):
        for mac in self._selected_macs():
            self._get_status_one(mac)

    def _get_status_one(self, mac):
        if mac in self.busy_macs:
            return
        self.busy_macs.add(mac)
        self._set_cell(mac, 'status', 'checking...')
        threading.Thread(target=self._get_status_worker, args=(mac,), daemon=True).start()

    def _get_status_worker(self, mac):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            mode_hex = lhv2.readMode().hex()
            try:
                power_label = decodePowerState(lhv2.readPowerState())
            except Exception:
                power_label = None
            firmware = lhv2.readFirmwareRevision()
            lhv2.disconnect()
            self.events.put(('status_done', (mac, mode_hex, power_label, firmware)))
        except Exception as e:
            self.events.put(('status_failed', (mac, str(e))))

    # ---- identify -------------------------------------------------------------------
    def _identify_selected(self):
        for mac in self._selected_macs():
            self._identify_one(mac)

    def _identify_one(self, mac):
        if mac in self.busy_macs:
            return
        self.busy_macs.add(mac)
        self._set_cell(mac, 'status', 'Identifying...')
        self.status_var.set(f'Identifying {mac}...')
        threading.Thread(target=self._identify_worker, args=(mac,), daemon=True).start()

    def _identify_worker(self, mac):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            lhv2.identify()
            lhv2.disconnect()
            self.events.put(('identify_done', (mac,)))
        except IdentifyNotSupported as e:
            self.events.put(('identify_unsupported', (mac, str(e))))
        except Exception as e:
            self.events.put(('identify_failed', (mac, str(e))))

    # ---- rename / remove --------------------------------------------------------------
    def _rename_selected(self):
        macs = self._selected_macs()
        if not macs:
            return
        if len(macs) > 1:
            messagebox.showinfo('Select one', 'Select exactly one station to rename.')
            return
        mac = macs[0]
        st = self.stations.get(mac)
        if not st or not st['name']:
            messagebox.showinfo('Not registered', 'Register this station first.')
            return
        new_name = simpledialog.askstring('Rename station', 'New name:', initialvalue=st['name'])
        if not new_name or new_name == st['name']:
            return
        registry.remove(st['name'])
        registry.add(new_name, mac, st['mode'], firmware=st['firmware'])
        st['name'] = new_name
        self._refresh_table()

    def _remove_selected(self):
        macs = self._selected_macs()
        if not macs:
            return
        label = macs[0] if len(macs) == 1 else f'{len(macs)} stations'
        if not messagebox.askyesno('Remove', f'Remove {label}?'):
            return
        for mac in macs:
            st = self.stations.get(mac)
            if st and st['name']:
                registry.remove(st['name'])
            self.stations.pop(mac, None)
        self._refresh_table()

    # ---- event loop (main thread only) ---------------------------------------------
    def _poll_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, kind, payload):
        if kind == 'scan_done':
            new_count = 0
            for mac in payload:
                if mac not in self.stations:
                    self.stations[mac] = self._blank_station(mac)
                    new_count += 1
            self._refresh_table()
            self.status_var.set(f'Scan complete: {new_count} new station(s) found')
            self.scan_btn.config(state='normal')
            self._scan_active = False
            self.scan_progress['value'] = 0
        elif kind == 'scan_error':
            self.status_var.set('Scan failed')
            self.scan_btn.config(state='normal')
            self._scan_active = False
            self.scan_progress['value'] = 0
            messagebox.showerror('Scan failed', payload)
        elif kind == 'register_ready':
            mac, adv_name, mode_hex, firmware = payload
            self.status_var.set('Ready')
            self._open_register_dialog(mac, adv_name, mode_hex, firmware)
        elif kind == 'register_read_failed':
            mac, err = payload
            self.status_var.set('Ready')
            if messagebox.askyesno(
                    'Could not read station',
                    f'Could not connect to {mac} ({err}).\nRegister anyway without a suggested mode?'):
                self._open_register_dialog(mac, mac, None)
        elif kind == 'power_done':
            mac, turn_on, power_label = payload
            self.busy_macs.discard(mac)
            self.status_var.set(f'{mac}: powered {"on" if turn_on else "off"}')
            if power_label:
                self._set_cell(mac, 'status', power_label)
        elif kind == 'power_failed':
            mac, turn_on, err = payload
            self.busy_macs.discard(mac)
            self._set_cell(mac, 'status', 'unreachable')
            self.status_var.set(f'{mac}: unreachable ({err})')
        elif kind == 'identify_done':
            (mac,) = payload
            self.busy_macs.discard(mac)
            self.status_var.set(f'{mac}: identify sent')
        elif kind == 'identify_unsupported':
            mac, err = payload
            self.busy_macs.discard(mac)
            self._set_cell(mac, 'status', 'n/a')
            self.status_var.set(f'{mac}: identify not supported')
            messagebox.showinfo('Identify not supported', f'{mac}: {err}')
        elif kind == 'identify_failed':
            mac, err = payload
            self.busy_macs.discard(mac)
            self._set_cell(mac, 'status', 'unreachable')
            self.status_var.set(f'{mac}: unreachable ({err})')
        elif kind == 'status_done':
            mac, mode_hex, power_label, firmware = payload
            self.busy_macs.discard(mac)
            self._set_cell(mac, 'status', power_label if power_label else 'n/a')
            self._set_cell(mac, 'firmware', firmware)
            if mode_hex:
                self._set_cell(mac, 'mode', int(mode_hex, 16))
            st = self.stations.get(mac)
            if st and st['name']:
                registry.update(st['name'], firmware=firmware)
            self.status_var.set(f'{mac}: status updated')
        elif kind == 'status_failed':
            mac, err = payload
            self.busy_macs.discard(mac)
            self._set_cell(mac, 'status', 'unreachable')
            self.status_var.set(f'{mac}: status check failed ({err})')


def main():
    root = tk.Tk()
    Lh2Gui(root)
    root.mainloop()


if __name__ == '__main__':
    main()
