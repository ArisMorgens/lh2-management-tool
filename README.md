## Power management tool for Lighthouse V2 base stations over Bluetooth LE

A desktop GUI for controlling Valve v2 lighthouse base stations over Bluetooth LE on Linux: scan for them, give them names, and wake them or put them to sleep.

### Setup

Install the `bluepy` build dependencies (Ubuntu):
```bash
sudo apt-get install -y libglib2.0-dev libbluetooth-dev build-essential python3-dev python3-tk
```

Create a virtual environment and install `bluepy` (Bluetooth) and `pyserial` (USB channel setting):
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install bluepy 'pyserial~=3.5'
```

Setting the channel over USB needs access to the serial port; if it's denied, add yourself to the `dialout` group and log in again:
```bash
sudo usermod -aG dialout $USER
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

Click **Scan** to find nearby base stations (`LHB-*`), select one and click **Register** to give it a name, then use **Wake** / **Sleep** / **Get Status** / **Identify** on any selected station(s).

**Set Channel (USB)...** sets the channel of a single base station plugged in with a USB data cable, and checks that the station confirms saving it. Its Mode is re-read over Bluetooth after the channel is set.

### What the Mode, Status and Health columns show
They are read live over Bluetooth, from the station's radio chip. They describe what the radio reports, which isn't always what the station is actually doing:

- **Mode** matches the station's channel on radio firmware 2.x. On older radio firmware (`R: 1.1...` in the Firmware column) it shows a leftover value (e.g. 234 or 138); the station still transmits on its correct channel.
- **Status "Not set since boot"** means the radio hasn't been told a power state since it powered up (firmware 1.1 and 2.2). The station is running; sending **Wake** or **Sleep** once gives a real status.
- **Health (experimental)** comes from the `faults` field of the station's info block (the [OOTX base station info](https://github.com/nairol/LighthouseRedox/blob/master/docs/Base%20Station.md#base-station-info-block)), which is 0 on a healthy station. **Fault** shows the raw value, as the individual bits aren't documented. That a non-zero value means a blinking red LED is based on our own observations of a few stations, not on Valve documentation. Radio firmware 1.1 stations don't expose the info block, so their health shows **–**.
- To restart a station safely, put it to **Sleep** (it spins down), wait a few seconds, then unplug the power for at least 10 seconds. Don't use the USB console's `reboot` command: it restarts only part of the station and can leave the USB console unresponsive until the next power cycle.

### Also in this repo
- [lh2diag.py](/src/lh2diag.py) -- dumps every BLE service/characteristic a station exposes, useful for troubleshooting a misbehaving unit.

### Resources
- [lighthouse-v2-manager](https://github.com/nouser2013/lighthouse-v2-manager/) -- @nouser2013's Windows implementation of the v2 power-management protocol, and the original reverse-engineering this project is based on.
- [lh2ctrl](https://github.com/risa2000/lh2ctrl) -- the original project by @risa2000.
- [crazyflie-clients-python](https://github.com/bitcraze/crazyflie-clients-python) -- cfclient's `basestation_mode_dialog.py` is the reference for the USB channel commands (`mode N`, `param save`, `id`).
- [Lighthouse 2.0 GATT Information](https://gist.github.com/BenWoodford/3a1e500a4ea2673525f5adb4120fd47c) -- documents the mode/power/identify characteristic values this tool relies on.
