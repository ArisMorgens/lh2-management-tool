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
from lhusb import LhUsb, find_ports as find_usb_ports, CHANNEL_MIN, CHANNEL_MAX

SCAN_TIMEOUT = 8
# at startup: find which registered stations are on, and their MAC addresses
STARTUP_SCAN_TIMEOUT = 5
# before acting on stations no scan has found yet in this session
QUICK_SCAN_TIMEOUT = 4
SCAN_TICK_MS = 100
INTERFACE = 0
TRY_COUNT = 3
TRY_PAUSE = 2
# right after Wake a sleeping station briefly reports a transitional power value
# (0x08, measured on radio firmware 2.9) before Awake, so wait before reading back
WAKE_SETTLE = 1

COLUMNS = ('name', 'mode', 'id', 'firmware', 'status', 'health')
COLUMN_LABELS = ('Name', 'Mode ⓘ', 'ID', 'Firmware', 'Status ⓘ', 'Health ⓘ')
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
        "sending Wake or Sleep once gives a real status.\n\n"
        "\"not found\": the station wasn't heard in the last scan, so it's probably off "
        "or out of range. Scan again once it's on."),
    'health': (
        "Experimental.\n\n"
        "Read from the 'faults' field of the station's info block, documented as fault "
        "flags that are 0 on a healthy station. \"Fault\" shows the raw value; what the "
        "individual bits mean isn't documented.\n\n"
        "Radio firmware 1.1 stations don't report it (–)."),
}
# Shown when hovering a button, keyed by its label
BUTTON_TOOLTIPS = {
    'Scan': "Search for base stations nearby (about 8 s). New ones are listed below "
            "the line, ready to register. Registered stations that weren't found "
            "before are checked again.",
    'Wake': "Wake the selected stations: the rotor spins up and the lasers turn on.",
    'Sleep': "Put the selected stations to sleep: the lasers turn off and the rotor "
             "spins down.",
    'Get Status': "Read the selected stations' mode, firmware, power state and health "
                  "over Bluetooth.",
    'Identify': "Make the selected stations' LED blink so you can find them. Not "
                "supported on radio firmware 1.1.",
    'Register': "Give the selected new station a name. Only the name and the station's "
                "ID are saved; everything else is read live from the station.",
    'Rename': "Change the name of the selected registered station.",
    'Remove': "Remove the selected stations from the list and forget their names.",
    'Set Channel (USB)...': "Set the channel of the base station connected to this "
                            "computer with a USB data cable. Connect one station at a time.",
}
TOOLTIP_WRAP = 380


def healthLabel(faults):
    """Health column text for the info block's 'faults' byte (None: not reported)."""
    if faults is None:
        return '–'
    return 'OK' if faults == 0 else f'Fault (0x{faults:02x})'


def idFromBleName(ble_name):
    """Station ID from its BLE name: 'LHB-396F1AEE' -> '396F1AEE'."""
    if ble_name and ble_name.upper().startswith('LHB-'):
        return ble_name[4:].upper()
    return None


class Lh2Gui:
    def __init__(self, root):
        self.root = root
        self.root.title('Lighthouse Base Station Manager')
        self.events = queue.Queue()
        # station ID -> {'name','id','mac','mode','firmware','status','health'};
        # name=None means unregistered. Only name -> ID is persisted; 'mac' is set once
        # a scan in this session has heard the station, everything else is read live.
        self.stations = {}
        self.busy = set()  # keys of stations with a BLE operation in progress
        self._scan_active = False   # progress bar of the Scan button running
        self._scanning = False      # any scan running (startup, Scan, or before an action)
        self._pending = []          # (keys, action) waiting for a scan to find them
        self._usb_target = None
        self._busy_win = None

        self._build_ui()
        self._load_registered()
        self._start_startup_scan()
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
        ttk.Separator(btns).pack(fill='x', pady=6)
        self.usb_btn = ttk.Button(btns, text='Set Channel (USB)...', command=self._start_usb_channel)
        self.usb_btn.pack(fill='x', pady=2)
        for btn in btns.winfo_children():
            if isinstance(btn, ttk.Button):
                self._add_tooltip(btn, BUTTON_TOOLTIPS[btn.cget('text')])

        self.scan_progress = ttk.Progressbar(self.root, mode='determinate', maximum=100)
        self.scan_progress.pack(fill='x', padx=8)

        self.status_var = tk.StringVar(value='Ready')
        ttk.Label(self.root, textvariable=self.status_var, anchor='w').pack(fill='x', padx=8, pady=(0, 8))

    # ---- tooltips (column headings and buttons) -----------------------------------
    def _on_tree_motion(self, event):
        col = None
        if self.tree.identify_region(event.x, event.y) == 'heading':
            idx = int(self.tree.identify_column(event.x).lstrip('#')) - 1
            col = COLUMNS[idx] if 0 <= idx < len(COLUMNS) else None
        text = COLUMN_TOOLTIPS.get(col)
        if text:
            self._show_tooltip(text, event.x_root, event.y_root)
        else:
            self._hide_tooltip()

    def _add_tooltip(self, widget, text):
        widget.bind('<Enter>', lambda e: self._show_tooltip(text, e.x_root, e.y_root))
        widget.bind('<Motion>', lambda e: self._show_tooltip(text, e.x_root, e.y_root))
        widget.bind('<Leave>', lambda _e: self._hide_tooltip())
        widget.bind('<ButtonPress>', lambda _e: self._hide_tooltip(), add='+')

    def _show_tooltip(self, text, x_root, y_root):
        """Show `text` next to the pointer, or just move the tooltip if it's already shown."""
        x, y = x_root + 12, y_root + 16
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
            key = (info.get('id') or '').upper()
            if not key:
                continue
            st = self.stations.setdefault(key, self._blank_station(key))
            st['name'] = name
            st['status'] = 'searching...'
        self._refresh_table()

    @staticmethod
    def _blank_station(station_id, mac=None):
        return {'name': None, 'id': station_id, 'mac': mac, 'mode': None, 'firmware': None,
                'status': None, 'health': None}

    def _refresh_table(self):
        selection = self.tree.selection()
        self.tree.delete(*self.tree.get_children())

        registered = sorted(
            ((key, st) for key, st in self.stations.items() if st['name']),
            key=lambda item: item[1]['name'].lower())
        unregistered = sorted(
            ((key, st) for key, st in self.stations.items() if not st['name']),
            key=lambda item: item[0])

        for key, st in registered:
            self._insert_row(key, st)
        if registered and unregistered:
            sep = '─' * 14
            self.tree.insert('', 'end', iid=SEPARATOR_IID, values=(sep,) * len(COLUMNS))
        for key, st in unregistered:
            self._insert_row(key, st)
        self.tree.selection_set([k for k in selection if self.tree.exists(k)])

    def _insert_row(self, key, st):
        self.tree.insert('', 'end', iid=key, values=(
            st['name'] or '', st['mode'] if st['mode'] is not None else '',
            st['id'] or '', st['firmware'] or '', st['status'] or '', st['health'] or ''))

    def _selected_keys(self):
        sel = [key for key in self.tree.selection() if key != SEPARATOR_IID]
        if not sel:
            messagebox.showinfo('No selection', 'Select one or more stations first.')
        return sel

    def _set_cell(self, key, col, text):
        if key in self.stations:
            self.stations[key][col] = text
        if self.tree.exists(key):
            vals = list(self.tree.item(key, 'values'))
            vals[COLUMNS.index(col)] = text
            self.tree.item(key, values=vals)

    def _label(self, key):
        """Name for messages: the registered name, else the station ID (or MAC)."""
        st = self.stations.get(key)
        return (st and st['name']) or key

    def _mac_for(self, key):
        """MAC to connect to, or None (and the row marked) if no scan has found it."""
        st = self.stations.get(key)
        if st and st['mac']:
            return st['mac']
        self._set_cell(key, 'status', 'not found')
        self.status_var.set(f'{self._label(key)}: not found')
        return None

    def _for_found(self, keys, action):
        """Run action(key) for each station; ones no scan has found yet are looked for first."""
        unseen = [k for k in keys if not (self.stations.get(k) or {}).get('mac')]
        for key in keys:
            if key not in unseen:
                action(key)
        if not unseen:
            return
        for key in unseen:
            self._set_cell(key, 'status', 'searching...')
        self._pending.append((unseen, action))
        if not self._scanning:  # otherwise the scan already running serves them
            self._scanning = True
            self.scan_btn.config(state='disabled')
            self.status_var.set('Looking for the selected stations...')
            threading.Thread(target=self._scan_worker, args=(QUICK_SCAN_TIMEOUT, 'quick_scan'),
                             daemon=True).start()

    def _serve_pending(self, heard):
        """After a scan: run the waiting actions on the stations it heard."""
        pending, self._pending = self._pending, []
        for keys, action in pending:
            for key in keys:
                if key in heard:
                    action(key)
                elif key in self.stations:
                    self._set_cell(key, 'status', 'not found')

    # ---- scan -------------------------------------------------------------------
    def _start_startup_scan(self):
        if not any(st['name'] for st in self.stations.values()):
            return
        self._scanning = True
        self.scan_btn.config(state='disabled')
        self.status_var.set('Looking for registered stations...')
        threading.Thread(target=self._scan_worker, args=(STARTUP_SCAN_TIMEOUT, 'startup_scan'),
                         daemon=True).start()

    def _start_scan(self):
        self._scanning = True
        self.scan_btn.config(state='disabled')
        self.status_var.set('Scanning...')
        self.scan_progress['value'] = 0
        self._scan_start = time.monotonic()
        self._scan_active = True
        self._tick_scan_progress()
        threading.Thread(target=self._scan_worker, args=(SCAN_TIMEOUT, 'scan_done'),
                         daemon=True).start()

    def _tick_scan_progress(self):
        if not self._scan_active:
            return
        elapsed = time.monotonic() - self._scan_start
        pct = min(100, (elapsed / SCAN_TIMEOUT) * 100)
        self.scan_progress['value'] = pct
        if pct < 100:
            self.root.after(SCAN_TICK_MS, self._tick_scan_progress)

    def _scan_worker(self, seconds, done_event):
        try:
            scanner = btle.Scanner()
            devices = scanner.scan(seconds)
            found = []
            for dev in devices:
                name = dev.getValueText(btle.ScanEntry.COMPLETE_LOCAL_NAME) or \
                    dev.getValueText(btle.ScanEntry.SHORT_LOCAL_NAME)
                station_id = idFromBleName(name)
                if station_id:
                    found.append((dev.addr.upper(), station_id))
            self.events.put((done_event, found))
        except Exception as e:
            self.events.put(('scan_error', str(e)))

    def _apply_scan(self, found, add_new):
        """Record the MACs of the stations heard; returns (keys heard, number of new stations)."""
        heard, new_count = set(), 0
        for mac, station_id in found:
            st = self.stations.get(station_id)
            if st is None:
                if not add_new:
                    continue
                st = self.stations[station_id] = self._blank_station(station_id)
                new_count += 1
            st['mac'] = mac
            heard.add(station_id)
        self._refresh_table()
        return heard, new_count

    # ---- register ---------------------------------------------------------------
    def _register_selected(self):
        keys = [k for k in self._selected_keys() if not self.stations[k]['name']]
        self._for_found(keys, self._register_one)

    def _register_one(self, key):
        mac = self._mac_for(key)
        if not mac:
            return
        self.status_var.set(f'Reading {self._label(key)}...')
        threading.Thread(target=self._register_worker, args=(key, mac), daemon=True).start()

    def _register_worker(self, key, mac):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            mode = lhv2.readMode()
            firmware = lhv2.readFirmwareRevision()
            lhv2.disconnect()
        except Exception as e:
            self.events.put(('register_read_failed', (key, str(e))))
            return
        self.events.put(('register_ready', (key, mode[0] if mode else None, firmware)))

    def _open_register_dialog(self, key, mode=None, firmware=None):
        st = self.stations.get(key)
        if not st or not st['id']:
            return
        ble_name = f"LHB-{st['id']}"
        name = simpledialog.askstring(
            'Register station', f'Name for {ble_name} (ID {st["id"]}):', initialvalue=ble_name)
        if not name:
            return
        registry.add(name, st['id'])
        st['name'] = name
        if mode is not None:
            st['mode'] = mode
        if firmware:
            st['firmware'] = firmware
        self._refresh_table()
        self.status_var.set(f'Registered {name}')

    # ---- power control ------------------------------------------------------------
    def _power_selected(self, turn_on):
        self._for_found(self._selected_keys(), lambda key: self._power_one(key, turn_on))

    def _power_one(self, key, turn_on):
        if key in self.busy:
            return
        mac = self._mac_for(key)
        if not mac:
            return
        self.busy.add(key)
        self._set_cell(key, 'status', 'Waking...' if turn_on else 'Going to sleep...')
        self.status_var.set(f'{"Waking" if turn_on else "Putting to sleep"} {self._label(key)}...')
        threading.Thread(target=self._power_worker, args=(key, mac, turn_on), daemon=True).start()

    def _power_worker(self, key, mac, turn_on):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            if turn_on:
                lhv2.powerOn()
                time.sleep(WAKE_SETTLE)
            else:
                lhv2.powerOff()
            try:
                power_label = decodePowerState(lhv2.readPowerState())
            except Exception:
                power_label = None
            lhv2.disconnect()
            self.events.put(('power_done', (key, turn_on, power_label)))
        except Exception as e:
            self.events.put(('power_failed', (key, turn_on, str(e))))

    # ---- status query --------------------------------------------------------------
    def _get_status_selected(self):
        self._for_found(self._selected_keys(), self._get_status_one)

    def _get_status_one(self, key):
        if key in self.busy:
            return
        mac = self._mac_for(key)
        if not mac:
            return
        self.busy.add(key)
        self._set_cell(key, 'status', 'checking...')
        threading.Thread(target=self._get_status_worker, args=(key, mac), daemon=True).start()

    def _get_status_worker(self, key, mac):
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
            self.events.put(('status_done', (key, mode[0] if mode else None,
                                             power_label, firmware, health)))
        except Exception as e:
            self.events.put(('status_failed', (key, str(e))))

    # ---- identify -------------------------------------------------------------------
    def _identify_selected(self):
        self._for_found(self._selected_keys(), self._identify_one)

    def _identify_one(self, key):
        if key in self.busy:
            return
        mac = self._mac_for(key)
        if not mac:
            return
        self.busy.add(key)
        self._set_cell(key, 'status', 'Identifying...')
        self.status_var.set(f'Identifying {self._label(key)}...')
        threading.Thread(target=self._identify_worker, args=(key, mac), daemon=True).start()

    def _identify_worker(self, key, mac):
        try:
            lhv2 = LHV2(mac, INTERFACE, verbose=0)
            lhv2.connect(TRY_COUNT, TRY_PAUSE)
            lhv2.identify()
            lhv2.disconnect()
            self.events.put(('identify_done', (key,)))
        except IdentifyNotSupported as e:
            self.events.put(('identify_unsupported', (key, str(e))))
        except Exception as e:
            self.events.put(('identify_failed', (key, str(e))))

    # ---- channel over USB ----------------------------------------------------------
    def _start_usb_channel(self):
        self._usb_target = None
        self.usb_btn.config(state='disabled')
        self.status_var.set('Reading the base station on USB...')
        self._show_busy('Reading the base station on USB...')
        threading.Thread(target=self._usb_info_worker, daemon=True).start()

    def _usb_info_worker(self):
        try:
            ports = find_usb_ports()
            if not ports:
                raise RuntimeError('No base station found on USB. Plug one in with a data cable.')
            if len(ports) > 1:
                raise RuntimeError(f'{len(ports)} base stations on USB ({", ".join(ports)}). '
                                   'Connect only one at a time.')
            info = LhUsb(ports[0]).read_info()
            self.events.put(('usb_info', (ports[0], info)))
        except Exception as e:
            self.events.put(('usb_failed', str(e)))

    def _select_station(self, key):
        self.tree.selection_set(key)
        self.tree.focus(key)
        self.tree.see(key)

    def _ask_usb_channel(self, port, info):
        current = info['channel']
        current_text = 'unknown' if current is None else \
            '0 (not supported)' if current == 0 else str(current)
        # the ID the USB console reports is the same one the table is keyed by
        key = info['uid'].upper() if info['uid'] else None
        if key in self.stations:
            self._usb_target = key
            self._select_station(key)
            name_text = self.stations[key]['name'] or 'not registered'
        else:
            # don't leave an unrelated station highlighted
            self.tree.selection_set(())
            name_text = 'not in the list (Scan to find it)' if key else \
                'unknown (the station reported no ID)'
        channel = simpledialog.askinteger(
            'Set channel (USB)',
            f"Base station on {port}\nID: {info['uid'] or 'unknown'}\nName: {name_text}\n"
            f'Current channel: {current_text}\n\n'
            f'New channel ({CHANNEL_MIN}-{CHANNEL_MAX}):',
            initialvalue=current if current and CHANNEL_MIN <= current <= CHANNEL_MAX else CHANNEL_MIN,
            minvalue=CHANNEL_MIN, maxvalue=CHANNEL_MAX, parent=self.root)
        if channel is None:
            self.usb_btn.config(state='normal')
            self.status_var.set('Ready')
            return
        self.status_var.set(f'Setting channel {channel} on {port}...')
        self._show_busy(f'Setting channel {channel}...')
        threading.Thread(target=self._usb_set_worker, args=(port, channel), daemon=True).start()

    def _show_busy(self, text):
        """Modal window with a moving bar; blocks the main window until _hide_busy()."""
        self._hide_busy()
        win = tk.Toplevel(self.root)
        win.title('Set channel (USB)')
        win.transient(self.root)
        win.resizable(False, False)
        win.protocol('WM_DELETE_WINDOW', lambda: None)  # can't be closed while waiting
        ttk.Label(win, text=text).pack(padx=16, pady=(12, 6))
        bar = ttk.Progressbar(win, mode='indeterminate', length=240)
        bar.pack(padx=16, pady=(0, 12))
        bar.start(15)
        # centre it over the main window
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 2
        win.geometry(f'+{x}+{y}')
        win.grab_set()
        self._busy_win = win

    def _hide_busy(self):
        if self._busy_win:
            self._busy_win.grab_release()
            self._busy_win.destroy()
            self._busy_win = None

    def _usb_set_worker(self, port, channel):
        try:
            confirmed = LhUsb(port).set_channel(channel)
            self.events.put(('usb_set_done', (port, channel, confirmed)))
        except Exception as e:
            self.events.put(('usb_failed', str(e)))

    # ---- rename / remove --------------------------------------------------------------
    def _rename_selected(self):
        keys = self._selected_keys()
        if not keys:
            return
        if len(keys) > 1:
            messagebox.showinfo('Select one', 'Select exactly one station to rename.')
            return
        st = self.stations.get(keys[0])
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
        keys = self._selected_keys()
        if not keys:
            return
        label = self._label(keys[0]) if len(keys) == 1 else f'{len(keys)} stations'
        if not messagebox.askyesno('Remove', f'Remove {label}?'):
            return
        for key in keys:
            st = self.stations.get(key)
            if st and st['name']:
                registry.remove(st['name'])
            self.stations.pop(key, None)
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
        if kind == 'quick_scan':
            heard, _ = self._apply_scan(payload, add_new=False)
            self._scanning = False
            self.scan_btn.config(state='normal')
            self.status_var.set('Ready')
            self._serve_pending(heard)
        elif kind == 'startup_scan':
            heard, _ = self._apply_scan(payload, add_new=False)
            self._scanning = False
            # only connect to registered stations that are on; the rest are marked at once
            for key, st in list(self.stations.items()):
                if not st['name']:
                    continue
                if key in heard:
                    self._get_status_one(key)
                else:
                    self._set_cell(key, 'status', 'not found')
            missing = sum(1 for k, st in self.stations.items() if st['name'] and k not in heard)
            self.status_var.set(f'{len(heard)} registered station(s) found'
                                + (f', {missing} not found' if missing else ''))
            self.scan_btn.config(state='normal')
            self._serve_pending(heard)
        elif kind == 'scan_done':
            heard, new_count = self._apply_scan(payload, add_new=True)
            self._scanning = False
            self._serve_pending(heard)
            # registered stations that were missing before: check them now that they're heard
            for key in heard:
                st = self.stations[key]
                if st['name'] and st['status'] in (None, 'not found', 'searching...'):
                    self._get_status_one(key)
            self.status_var.set(f'Scan complete: {new_count} new station(s) found')
            self.scan_btn.config(state='normal')
            self._scan_active = False
            self.scan_progress['value'] = 0
        elif kind == 'scan_error':
            self._scanning = False
            self._serve_pending(set())
            self.status_var.set('Scan failed')
            self.scan_btn.config(state='normal')
            self._scan_active = False
            self.scan_progress['value'] = 0
            for key, st in self.stations.items():
                if st['status'] == 'searching...':
                    self._set_cell(key, 'status', '')
            messagebox.showerror('Scan failed', payload)
        elif kind == 'register_ready':
            key, mode, firmware = payload
            self.status_var.set('Ready')
            self._open_register_dialog(key, mode, firmware)
        elif kind == 'register_read_failed':
            key, err = payload
            self.status_var.set('Ready')
            if messagebox.askyesno(
                    'Could not read station',
                    f'Could not connect to {self._label(key)} ({err}).\nRegister anyway?'):
                self._open_register_dialog(key)
        elif kind == 'power_done':
            key, turn_on, power_label = payload
            self.busy.discard(key)
            self.status_var.set(f'{self._label(key)}: {"woken" if turn_on else "put to sleep"}')
            if power_label:
                self._set_cell(key, 'status', power_label)
        elif kind == 'power_failed':
            key, turn_on, err = payload
            self.busy.discard(key)
            self._set_cell(key, 'status', 'unreachable')
            self.status_var.set(f'{self._label(key)}: unreachable ({err})')
        elif kind == 'identify_done':
            (key,) = payload
            self.busy.discard(key)
            self.status_var.set(f'{self._label(key)}: identify sent')
        elif kind == 'identify_unsupported':
            key, err = payload
            self.busy.discard(key)
            self._set_cell(key, 'status', 'n/a')
            self.status_var.set(f'{self._label(key)}: identify not supported')
            messagebox.showinfo('Identify not supported', f'{self._label(key)}: {err}')
        elif kind == 'identify_failed':
            key, err = payload
            self.busy.discard(key)
            self._set_cell(key, 'status', 'unreachable')
            self.status_var.set(f'{self._label(key)}: unreachable ({err})')
        elif kind == 'status_done':
            key, mode, power_label, firmware, health = payload
            self.busy.discard(key)
            self._set_cell(key, 'status', power_label if power_label else 'n/a')
            self._set_cell(key, 'firmware', firmware)
            self._set_cell(key, 'health', health)
            if mode is not None:
                self._set_cell(key, 'mode', mode)
            self.status_var.set(f'{self._label(key)}: status updated')
        elif kind == 'status_failed':
            key, err = payload
            self.busy.discard(key)
            self._set_cell(key, 'status', 'unreachable')
            self.status_var.set(f'{self._label(key)}: status check failed ({err})')
        elif kind == 'usb_info':
            self._hide_busy()
            self._ask_usb_channel(*payload)
        elif kind == 'usb_set_done':
            port, channel, confirmed = payload
            self._hide_busy()
            self.usb_btn.config(state='normal')
            if confirmed != channel:
                self.status_var.set(f'{port}: channel not confirmed')
                messagebox.showerror('Set channel failed',
                                     f'Asked for channel {channel}, station reports {confirmed}. Try again.')
                return
            self.status_var.set(f'{port}: channel set to {channel}')
            st = self.stations.get(self._usb_target)
            if st and st['mac']:
                # re-read over BLE so the Mode column shows what the station now reports
                self._get_status_one(self._usb_target)
            messagebox.showinfo('Channel set', f'Channel {channel} saved on the station.')
        elif kind == 'usb_failed':
            self._hide_busy()
            self.usb_btn.config(state='normal')
            self.status_var.set('USB: failed')
            messagebox.showerror('USB base station', payload)


def main():
    root = tk.Tk()
    Lh2Gui(root)
    root.mainloop()


if __name__ == '__main__':
    main()
