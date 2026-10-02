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

COLUMNS = ('name', 'mode', 'mac', 'firmware', 'status', 'health')
COLUMN_LABELS = ('Name', 'Mode ⓘ', 'MAC', 'Firmware', 'Status ⓘ', 'Health ⓘ')
SEPARATOR_IID = '__separator__'

# Shown when hovering the column heading. What the radio reports depends on its
# firmware (the "R:" part of the Firmware column).
COLUMN_TOOLTIPS = {
    'mode': (
        "The channel as reported by the station's Bluetooth radio.\n\n"
        "Radio firmware 2.x: always matches the station's channel.\n\n"
        "Radio firmware 1.1: shows a leftover value (e.g. 234). The base station still runs on its correct channel."),
    'status': (
        "The power state as reported by the station's Bluetooth radio.\n\n"
        "\"Not set since boot\" (radio firmware 1.1 and 2.2): the radio hasn't been told a "
        "power state since the station was powered up. The station is running; "
        "sending Wake or Sleep once gives a real status."),
    'health': (
        "Experimental.\n\n"
        "Read from the 'faults' field of the station's info block, documented as fault "
        "flags that are 0 on a healthy station. \"Fault\" shows the raw value; what the "
        "individual bits mean isn't documented.\n\n"
        "Radio firmware 1.1 stations don't report it (–)."),
}
TOOLTIP_WRAP = 380


def healthLabel(faults):
    """Health column text for the info block's 'faults' byte (None: not reported)."""
    if faults is None:
        return '–'
    return 'OK' if faults == 0 else f'Fault (0x{faults:02x})'


class Lh2Gui:
    def __init__(self, root):
        self.root = root
        self.root.title('Lighthouse Base Station Manager')
        self.events = queue.Queue()
        # mac -> {'name','mode','mac','firmware','status','health'}; name=None means unregistered.
        # Only name -> MAC is persisted; everything else is read live over BLE.
        self.stations = {}
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
        self._tooltip = None
        self._tooltip_text = None
        self.tree.bind('<Motion>', self._on_tree_motion)
        self.tree.bind('<Leave>', lambda _e: self._hide_tooltip())

        btns = ttk.Frame(main_frame)
        btns.pack(side='left', fill='y', padx=8)
        self.scan_btn = ttk.Button(btns, text='Scan', command=self._start_scan)
        self.scan_btn.pack(fill='x', pady=2)
        ttk.Separator(btns).pack(fill='x', pady=6)
        ttk.Button(btns, text='Wake', command=lambda: self._power_selected(True)).pack(fill='x', pady=2)
        ttk.Button(btns, text='Sleep', command=lambda: self._power_selected(False)).pack(fill='x', pady=2)
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

    # ---- heading tooltips --------------------------------------------------------
    def _on_tree_motion(self, event):
        col = None
        if self.tree.identify_region(event.x, event.y) == 'heading':
            idx = int(self.tree.identify_column(event.x).lstrip('#')) - 1
            col = COLUMNS[idx] if 0 <= idx < len(COLUMNS) else None
        text = COLUMN_TOOLTIPS.get(col)
        if not text:
            self._hide_tooltip()
            return
        x, y = event.x_root + 12, event.y_root + 16
        if self._tooltip and self._tooltip_text == text:
            self._tooltip.geometry(f'+{x}+{y}')
            return
        self._hide_tooltip()
        self._tooltip = tk.Toplevel(self.root)
        self._tooltip.wm_overrideredirect(True)
        self._tooltip.geometry(f'+{x}+{y}')
        tk.Label(self._tooltip, text=text, justify='left', wraplength=TOOLTIP_WRAP,
                 relief='solid', borderwidth=1, background='#ffffe0', padx=6, pady=4).pack()
        self._tooltip_text = text

    def _hide_tooltip(self):
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None

    # ---- table state ----------------------------------------------------------
    def _load_registered(self):
        for name, info in registry.all().items():
            mac = info['mac']
            st = self.stations.setdefault(mac, self._blank_station(mac))
            st['name'] = name
        self._refresh_table()

    def _refresh_all_statuses(self):
        for mac, st in self.stations.items():
            if st['name']:
                self._get_status_one(mac)

    @staticmethod
    def _blank_station(mac):
        return {'name': None, 'mode': None, 'mac': mac, 'firmware': None, 'status': None,
                'health': None}

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
            mac, st['firmware'] or '', st['status'] or '', st['health'] or ''))

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
            mode = lhv2.readMode()
            firmware = lhv2.readFirmwareRevision()
            adv_name = lhv2.getName()
            lhv2.disconnect()
        except Exception as e:
            self.events.put(('register_read_failed', (mac, str(e))))
            return
        self.events.put(('register_ready', (mac, adv_name, mode[0] if mode else None, firmware)))

    def _open_register_dialog(self, mac, adv_name, mode=None, firmware=None):
        st = self.stations.setdefault(mac, self._blank_station(mac))
        name = simpledialog.askstring(
            'Register station', f'Name for {adv_name or mac} ({mac}):', initialvalue=adv_name or mac)
        if not name:
            return
        registry.add(name, mac)
        st['name'] = name
        if mode is not None:
            st['mode'] = mode
        if firmware:
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
        self._set_cell(mac, 'status', 'Waking...' if turn_on else 'Going to sleep...')
        self.status_var.set(f'{"Waking" if turn_on else "Putting to sleep"} {mac}...')
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
            try:
                power_label = decodePowerState(lhv2.readPowerState())
            except Exception:
                power_label = None
            firmware = lhv2.readFirmwareRevision()
            mode = lhv2.readMode()
            try:
                health = healthLabel(lhv2.readFaults())
            except Exception:
                health = healthLabel(None)
            lhv2.disconnect()
            self.events.put(('status_done', (mac, mode[0] if mode else None,
                                             power_label, firmware, health)))
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
        registry.rename(st['name'], new_name)
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
            mac, adv_name, mode, firmware = payload
            self.status_var.set('Ready')
            self._open_register_dialog(mac, adv_name, mode, firmware)
        elif kind == 'register_read_failed':
            mac, err = payload
            self.status_var.set('Ready')
            if messagebox.askyesno(
                    'Could not read station',
                    f'Could not connect to {mac} ({err}).\nRegister anyway?'):
                self._open_register_dialog(mac, None)
        elif kind == 'power_done':
            mac, turn_on, power_label = payload
            self.busy_macs.discard(mac)
            self.status_var.set(f'{mac}: {"woken" if turn_on else "put to sleep"}')
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
            mac, mode, power_label, firmware, health = payload
            self.busy_macs.discard(mac)
            self._set_cell(mac, 'status', power_label if power_label else 'n/a')
            self._set_cell(mac, 'firmware', firmware)
            self._set_cell(mac, 'health', health)
            if mode is not None:
                self._set_cell(mac, 'mode', mode)
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
