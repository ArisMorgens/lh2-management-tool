## Power management tool for Lighthouse V2 base stations over Bluetooth LE

A desktop GUI for controlling Valve v2 lighthouse base stations over Bluetooth LE on Linux: scan for them, give them names, and turn them on/off.

### Setup

Install the `bluepy` build dependencies (Ubuntu):
```bash
sudo apt-get install -y libglib2.0-dev libbluetooth-dev build-essential python3-dev python3-tk
```

Create a virtual environment and install `bluepy`:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install bluepy
```

Grant Bluetooth permissions so the tool can run without `sudo`:
```bash
sudo setcap cap_net_raw,cap_net_admin+eip $(find .venv -name bluepy-helper)
```

### Running the GUI
```bash
source .venv/bin/activate
python3 src/lh2gui.py
```

Click **Scan** to find nearby base stations (`LHB-*`), select one and click **Register** to give it a name, then use **On** / **Off** / **Get Status** / **Identify** on any selected station(s).

### Also in this repo
- [lh2diag.py](/src/lh2diag.py) -- dumps every BLE service/characteristic a station exposes, useful for troubleshooting a misbehaving unit.

### Resources
- [lighthouse-v2-manager](https://github.com/nouser2013/lighthouse-v2-manager/) -- @nouser2013's Windows implementation of the v2 power-management protocol, and the original reverse-engineering this project is based on.
- [lh2ctrl](https://github.com/risa2000/lh2ctrl) -- the original project by @risa2000.
- [Lighthouse 2.0 GATT Information](https://gist.github.com/BenWoodford/3a1e500a4ea2673525f5adb4120fd47c) -- documents the mode/power/identify characteristic values this tool relies on.
