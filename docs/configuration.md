# Configuration

The configuration file is located at `~/.config/g6_switcher/config.json`.  
The saved headphone volume level is stored separately in `~/.config/g6_switcher/state.json`.

You can generate and configure this file automatically using the interactive setup wizard:

```bash
python3 g6_switcher.py --setup
```

Or you can create and edit `config.json` manually. After making changes, reload the background service to apply them without interruption:

```bash
systemctl --user reload g6_switcher.service
```

---

## Default Configuration (`config.json`)

```json
{
    "log_level": "INFO",
    "presets": {
        "headphones": "Headphones",
        "speakers": "Speakers",
        "default": "Default"
    },
    "features": {
        "preset_tolerance_mode": "disabled",
        "use_safe_volume": false,
        "safe_volume_percent": 25,
        "save_headphones_volume": true,
        "use_dynamic_limiter": false,
        "use_hardware_profiles": false,
        "warn_on_dsp_boot": false
    },
    "dynamic_limiter_offsets": {
        "95": 0.0,
        "100": 5.0
    }
}
```

---

## Hardware Permissions (udev rules)

The script requires read/write access to the Sound BlasterX G6 `/dev/hidraw*` device nodes to monitor physical switch events.

To create the required udev rule and apply permissions immediately, run:

```bash
echo 'ACTION!="remove", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="041e", ATTRS{idProduct}=="3256", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/70-sound-blaster-g6.rules > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger
```

---

## Logging Level

| Key | Values | Description |
|---|---|---|
| `log_level` | `"INFO"`, `"DEBUG"`, `"WARNING"`, `"ERROR"` | Sets logging verbosity. Set to `"DEBUG"` when troubleshooting preset detection, HID packet reads, or volume events. |

---

## Presets

| Key | Description |
|---|---|
| `headphones` | Output preset applied when the G6 switches to headphone mode. |
| `speakers` | Output preset applied when the G6 switches to speaker (Line Out / S-PDIF) mode. |
| `default` | Empty / bypass output preset applied when switching the audio sink away from the G6 to another device. |

All three presets must already exist in your EasyEffects or PulseEffects installation before starting the script.

> **Important:** Do not bind these presets to any sound card inside EasyEffects or PulseEffects autoloading settings. Configuring such autoloading rules will conflict with the script.

---

## Feature settings

| Key | Description |
|---|---|
| `preset_tolerance_mode` | `disabled`, `once`, or `persistent`; controls whether a manually selected custom preset suppresses automatic switching. |
| `save_headphones_volume` | When `true`, saves the headphone volume level when switching away, and restores it when headphones are connected again. It also restores the previous speaker volume when switching back to speakers. |
| `use_safe_volume` | When `true` (and `save_headphones_volume` is `false`), enforces a fixed safe volume (`safe_volume_percent`) every time headphones are connected to protect your hearing. |
| `safe_volume_percent` | Target percentage (integer from `0` to `100`). Acts as the fixed volume when `use_safe_volume` is active, or as a safety fallback if `state.json` is missing or corrupted. |
| `use_dynamic_limiter` | Enables or disables volume-dependent limiter threshold adjustment for headphones. |
| `use_hardware_profiles` | Enables or disables automatic ALSA clock source and audio card profile switching. |
| `warn_on_dsp_boot` | Displays a desktop notification if the Sound BlasterX G6 boots into hardware DSP mode. |

### `preset_tolerance_mode` Behavior

- `disabled`: Always enforce automatic preset switching on every G6 output switch event.
- `once`: Ignore automatic preset switching once if a custom preset is active. Subsequent G6 output switch events will resume normal switching.
- `persistent`: Suspends automatic preset switching entirely as long as a custom preset (any preset other than `presets.headphones` or `presets.speakers`) remains loaded. Automatic switching resumes as soon as you manually select either the configured headphone or speaker preset in your effects app.

### Why use `use_hardware_profiles`?

> **Note:** This feature is supported only on **PipeWire** with **WirePlumber** and requires `amixer` (`alsa-utils`). It is automatically disabled on standalone PulseAudio systems.

The optical (S/PDIF) output of the Sound BlasterX G6 is driven by its internal DSP clock, which hardware-locks optical sample rates to a maximum of 48000 Hz / 24-bit. If you connect external speakers or an AV receiver via optical out, but want your headphones to run in **Direct Mode** (using the `Stereo Direct` clock to unlock pristine 384000 Hz / 32-bit playback), this feature manages that transition automatically:

- **Headphones Mode**: Sets ALSA clock source to `Stereo Direct` and card profile to `output:analog-stereo`.
- **Speakers Mode**: Sets ALSA clock source to `DSP Clock` and card profile to `output:iec958-stereo`.

#### WirePlumber Configuration for Optical Output

Due to G6 hardware constraints, the optical (S/PDIF) output **will produce no sound at all** unless WirePlumber is explicitly configured to restrict optical output properties to 48000 Hz and 24-bit depth.

First, check your installed WirePlumber version:

```bash
wireplumber --version
```

Then create **one** of the following configuration files manually depending on your version:

**For WirePlumber >= 0.5** (`~/.config/wireplumber/wireplumber.conf.d/51-g6-optical.conf`):

```spa-json
monitor.alsa.rules = [
  {
    matches = [
      {
        node.name = "~alsa_output.usb-Creative_.*iec958-stereo"
      }
    ]
    actions = {
      update-props = {
        audio.rate = 48000
        audio.allowed-rates = [ 44100 48000 ]
        audio.format = "S24LE"
      }
    }
  }
]
```

**For WirePlumber 0.4.x** (`~/.config/wireplumber/main.lua.d/51-g6-optical.lua`):

```lua
rule = {
  matches = {
    {
      { "node.name", "matches", "~alsa_output.usb-Creative_.*iec958-stereo" },
    },
  },
  apply_properties = {
    ["audio.rate"] = 48000,
    ["audio.allowed-rates"] = "44100,48000",
    ["audio.format"] = "S24LE",
  },
}
table.insert(alsa_monitor.rules, rule)
```

Restart WirePlumber to apply:

```bash
systemctl --user restart wireplumber
```

### Why use `warn_on_dsp_boot`?

Due to a known firmware quirk on cold boot or USB power cycles, the Sound BlasterX G6 occasionally fails to restore Direct Mode and silently falls back to hardware DSP mode (capping available hardware sample rates to 44.1/48 kHz). 

When this feature is enabled, the daemon inspects `/proc/asound/cardX/stream0` at startup. If high sample rate support (`384000 Hz`) is missing, it sends a desktop notification reminding you to press and hold the physical **Scout / Direct Mode** button on the side of the G6 for 2 seconds to re-engage Direct Mode.

---

## Dynamic Limiter

The dynamic limiter automatically adjusts the Limiter plugin's threshold in EasyEffects or PulseEffects based on your current system volume to prevent clipping and distortion at higher listening levels.

The `dynamic_limiter_offsets` section maps volume percentages to threshold attenuation offsets in dB:

```json
"dynamic_limiter_offsets": {
    "95": 0.0,
    "100": 5.0
}
```

- Keys represent system volume levels (from `0` to `150`).
- Values represent negative threshold offsets in dB (`5.0` applies a `-5.0 dB` limiter threshold).
- Offsets are linearly interpolated between defined points.

A Limiter filter must already be added and enabled inside your headphone preset. The script does not add or enable it.

> **Important:** To use the Limiter effectively for clipping protection without boosting quiet sounds, make sure **Threshold Boost** is disabled in the plugin's settings.

*Dynamic Limiter control is unavailable with EasyEffects 8.0.0–8.0.6 and is disabled automatically on those versions.*
