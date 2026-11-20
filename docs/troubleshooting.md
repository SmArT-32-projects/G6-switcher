# Troubleshooting

## General checks

### Review known limitations

Before troubleshooting, carefully review the [Known Limitations](https://github.com/SmArT-32-projects/G6-switcher/tree/main#known-limitations) section to make sure the observed behavior is not expected.

### Check service status and logs

Check whether the background service is running:

```bash
systemctl --user status g6_switcher.service
```

To enable detailed debug output, set `"log_level": "DEBUG"` in `~/.config/g6_switcher/config.json` and reload the service:

```bash
systemctl --user reload g6_switcher.service
```

View the 100 most recent log entries:

```bash
journalctl --user -u g6_switcher.service -n 100 --no-pager
```

To follow logs in real time, add the `-f` flag:

```bash
journalctl --user -u g6_switcher.service -f
```

### Check whether the issue is related to G6 switcher

If applicable, temporarily stop the service and try to reproduce the issue under the same conditions:

```bash
systemctl --user stop g6_switcher.service
```

---

## G6 is not detected

- Confirm the USB device ID:  
  ```bash
  lsusb -d 041e:3256
  ```

- Check if ALSA recognizes the card:  
  ```bash
  cat /proc/asound/cards
  ```
  
  Ensure `Sound BlasterX G6` appears in the output list.

- Verify `/dev/hidraw*` permissions:
  
  The script requires access to the G6 HID interfaces to track physical switches. Run `python3 g6_switcher.py --setup` to check permissions or [apply the udev rule](configuration.md#hardware-permissions-udev-rules) manually.

- Check device firmware version:
  
  Ensure the device firmware is updated to the latest version. Older firmware releases have known bugs on Linux. Firmware updates must be performed using the official Creative software on Windows.

- Try disconnecting and reconnecting the USB cable.

---

## Presets do not switch

- Make sure the Sound BlasterX G6 is selected as the default system audio output.

- Check if `preset_tolerance_mode` is set to `persistent` or `once` in `~/.config/g6_switcher/config.json`. If a custom preset is currently loaded in EasyEffects/PulseEffects, the script intentionally suppresses automatic preset switching until you select the configured headphone or speaker preset manually.

- Check that all three configured preset names (`headphones`, `speakers`, `default`) exactly match presets in EasyEffects or PulseEffects (case-sensitive).

---

## Hardware profiles do not switch

- This feature is exclusively supported on **PipeWire** with **WirePlumber** and requires `amixer` (`alsa-utils`). It is automatically disabled under standalone PulseAudio.
- Check available card profiles:
  ```bash
  pactl list cards
  ```
  Ensure the card provides `output:analog-stereo` and `output:iec958-stereo`.
- Check the ALSA clock source mixer control:
  
  Open `alsamixer` (press `F6` to select the Sound BlasterX G6).
  
  Verify that the control exists and lists both `Stereo Direct` (used for Headphones) and `DSP Clock` (used for Speakers / S-PDIF).
- Make sure the required WirePlumber rule file is created and loaded. See [WirePlumber Configuration](configuration.md#wireplumber-configuration-for-optical-output).

---

## Report a bug

Before submitting a bug report:
1. Set `"log_level": "DEBUG"` in `~/.config/g6_switcher/config.json` and reload the service:
   
   ```bash
   systemctl --user reload g6_switcher.service
   ```
1. Collect the recent debug logs:
   
   ```bash
   journalctl --user -u g6_switcher.service -n 100 --no-pager
   ```

Submit your report using the [GitHub Issue Tracker](https://github.com/SmArT-32-projects/g6-switcher/issues/new?template=bug_report.md).
