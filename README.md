# Sound BlasterX G6 Switcher

Linux utility that automatically detects the active output of the **Creative Sound BlasterX G6** and switches the EasyEffects/PulseEffects output presets. Optional features provide headphone-volume management, dynamic Limiter adjustment and G6 hardware-profile switching.

**Supported hardware: Sound BlasterX G6 only (USB VID:PID `041e:3256`).** Other Sound Blaster models and USB audio devices are not supported.

## Features

- Automatic headphones/speakers preset switching.
- Default bypass / clean preset applied when switching away from the G6 to other outputs.
- Optional headphone-volume save/restore and safe-volume handling.
- Optional volume-dependent Limiter threshold adjustment.
- Optional analog/optical hardware-profile switching (allows using the G6 optical output while keeping Direct Mode for headphones).
- Setup wizard and optional systemd user service.

## Requirements

- Creative Sound BlasterX G6 (`041e:3256`) with accessible `/dev/hidraw*` nodes.
- Python 3.7+.
- PipeWire with `pipewire-pulse`, or PulseAudio (`pactl` is required).
- EasyEffects 6.1.0+ or PulseEffects 4.8.1+ (both native and flatpak installations are supported).
- Three existing output presets: headphones, speakers, and a default preset (an empty/bypass preset for other outputs).

### Dependencies for optional features:

- `gsettings` from GLib for PulseEffects and GTK-based EasyEffects versions before 8.0.0.
- `alsa-utils` (`amixer`) and WirePlumber 0.4+ for G6 hardware-profile switching.
- `notify-send` for desktop notifications.
- `usbreset` for recovery attempt when the initial G6 HID state cannot be read.

The script uses the Python standard library; no `pip` installation is needed.

## Install

Download `g6_switcher.py` or download and unpack the archive from the [latest GitHub Release](https://github.com/SmArT-32-projects/g6-switcher/releases/latest) and run:

```bash
python3 g6_switcher.py --setup
```

Create the three output presets in EasyEffects/PulseEffects before setup. The script switches between existing presets; it does not create them. Do not bind them to a specific sound card in EasyEffects/PulseEffects autoloading rules.

The wizard checks HID permissions, creates `~/.config/g6_switcher/config.json`, and offers to install the script as a systemd user service.

You can decline the service installation and keep the script where it is to run it manually:

```bash
python3 g6_switcher.py
```

## Service

Show the current status:

```bash
systemctl --user status g6_switcher.service
journalctl --user -xeu g6_switcher.service
```

Apply configuration (~/.config/g6_switcher/config.json) changes:

```bash
systemctl --user reload g6_switcher.service
```

## Update / uninstall

Download the new `g6_switcher.py`, then run:

```bash
python3 g6_switcher.py --update
```

Uninstall the installed copy:

```bash
~/.local/bin/g6_switcher --uninstall
```

The uninstaller asks whether to remove the configuration and prints a separate command if the udev rule needs to be removed.

## Known limitations

- It is assumed that only one G6 card is physically connected to your machine at the same time.  
- If both Flatpak and native builds are installed, the script selects the first matching application in this order: EasyEffects Flatpak, PulseEffects Flatpak, EasyEffects native, PulseEffects native. It does not currently provide a separate application-selection setting.
- Dynamic limiter control requires a Limiter filter that is already present and enabled in the headphone preset. The script cannot add or enable the filter automatically. For clipping protection, disable **Threshold Boost** in the Limiter settings.
- Dynamic limiter control is unavailable with EasyEffects 8.0.0–8.0.6; the script disables this feature for those versions.

## Docs and support

- [Configuration](docs/configuration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Bug reports and feature requests](https://github.com/SmArT-32-projects/g6-switcher/issues/new/choose)

This is an independent community project. It is not affiliated with or endorsed by Creative Technology Ltd.
