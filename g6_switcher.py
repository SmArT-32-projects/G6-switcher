#!/usr/bin/env python3

"""
Sound BlasterX G6 Switcher
https://github.com/SmArT-32-projects/G6-switcher

Copyright (c) 2026 SmArT-32-projects
MIT License

Requires:
  - Python >= 3.7
  - PipeWire (with pipewire-pulse) or PulseAudio (pulseaudio-utils)
  - EasyEffects >= 6.1.0 or PulseEffects >= 4.8.1

Optional / Feature-specific:
  - libnotify (notify-send for desktop notifications)
  - alsa-utils (amixer, required for 'use_hardware_profiles')
  - WirePlumber >= 0.4 (required for 'use_hardware_profiles')
  - gsettings (glib2, required for PulseEffects or EasyEffects < 8.0.0)
  - usbreset (for automatic hardware recovery on boot)
"""

import sys

if sys.version_info < (3, 7):
    sys.exit("This script requires Python 3.7 or newer. "
             f"You are running {sys.version.split()[0]}")

import argparse
import ctypes
import fcntl
import json
import logging
import os
import re
import select
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path

# Setup inotify via ctypes
libc = ctypes.CDLL(None, use_errno=True)
IN_NONBLOCK = 0x800
IN_CLOEXEC = 0x80000
IN_OPEN = 0x00000020
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000

try:
    inotify_init1 = libc.inotify_init1
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int

    inotify_add_watch = libc.inotify_add_watch
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int
except AttributeError:
    # Fallback in case of execution in a non-standard environment where functions are unavailable
    inotify_init1 = None
    inotify_add_watch = None

# === ARGUMENT PARSING & SETUP WIZARD ===
SCRIPT_VERSION = "1.0"

parser = argparse.ArgumentParser(
    prog="g6_switcher",
    description=f"'Sound BlasterX G6 Switcher v{SCRIPT_VERSION}'\n"
                f"Automatic output and EasyEffects (PulseEffects) audio profile management for Sound BlasterX G6.\n\n"
                f"GitHub: https://github.com/SmArT-32-projects/G6-switcher\n"
                f"Copyright (c) 2026 SmArT-32-projects | MIT License",
    epilog="Files and paths used by this script:\n"
           "  Configuration:                ~/.config/g6_switcher/config.json\n"
           "  State:                        ~/.config/g6_switcher/state.json\n"
           "  Conf for WirePlumber >= 0.5:  ~/.config/wireplumber/wireplumber.conf.d/51-g6-optical.conf\n"
           "  Conf for WirePlumber 0.4.x:   ~/.config/wireplumber/main.lua.d/51-g6-optical.lua\n"
           "  Systemd user service:         ~/.config/systemd/user/g6_switcher.service\n"
           "  Installed executable:         ~/.local/bin/g6_switcher\n\n"
           "Note: After manually editing config.json, restart the script or reload the systemd user service to apply the changes:\n"
           "  systemctl --user reload g6_switcher.service",
    formatter_class=argparse.RawTextHelpFormatter,
)
parser.add_argument('--setup', action='store_true', help='Run the initial configuration wizard')
parser.add_argument('--update', action='store_true', help='Update the installed system service with this script version')
parser.add_argument('--uninstall', action='store_true', help='Remove the script, service and all configuration files')
parser.add_argument('--daemon', action='store_true', help='Run script in daemon mode')
parser.add_argument('--version', action='version', version=f"Sound BlasterX G6 Switcher v{SCRIPT_VERSION}")
args = parser.parse_args()

def send_notification(message, icon="audio-card"):
    if shutil.which("notify-send"):
        if icon == "" or icon == None:
            icon="audio-card"
        try: 
            result = subprocess.run(
                ["notify-send", "Sound BlasterX G6 Switcher", message, "-i", icon],
                timeout=0.5, capture_output=True, text=True
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()
                msg = f"notify-send failed (code {result.returncode}): {err}" if err else f"notify-send failed (code {result.returncode})"
                if "logger" in globals():
                    logger.warning(msg)
                else:
                    print(f"[WARNING] {msg}", file=sys.stderr)
        except Exception as e:
            if "logger" in globals():
                logger.warning(f"notify-send exception: {e}")
            else:
                print(f"[WARNING] notify-send exception: {e}", file=sys.stderr)

def sd_notify(state):
    """Send notification to systemd manager via NOTIFY_SOCKET."""
    if args.daemon:
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        if not notify_socket:
            return
        try:
            if notify_socket.startswith("@"):
                notify_socket = "\0" + notify_socket[1:]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(notify_socket)
                sock.sendall(state.encode("utf-8"))
        except Exception:
            pass

def uninstall():
    print("=" * 60)
    print(" Sound BlasterX G6 Switcher Uninstaller ")
    print("=" * 60)

    if not prompt_bool("Are you sure you want to remove the service and the script itself?", False):
        print("Uninstallation aborted.")
        sys.exit(0)

    print("\nStopping and disabling Sound BlasterX G6 switcher service...")
    subprocess.run(["systemctl", "--user", "stop", "g6_switcher.service"], stderr=subprocess.DEVNULL)
    subprocess.run(["systemctl", "--user", "disable", "g6_switcher.service"], stderr=subprocess.DEVNULL)

    service_file = Path.home() / ".config" / "systemd" / "user" / "g6_switcher.service"
    if service_file.exists():
        service_file.unlink()
        print(f"Removed: {service_file}")

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, check=True)
    except Exception as e:
        print(f"Failed to execute systemctl: {e}")

    wp_conf_file = Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d" / "51-g6-optical.conf"
    wp_lua_file = Path.home() / ".config" / "wireplumber" / "main.lua.d" / "51-g6-optical.lua"
    
    wp_removed = False
    for wp_file in (wp_conf_file, wp_lua_file):
        if wp_file.exists():
            wp_file.unlink()
            print(f"Removed: {wp_file}")
            wp_removed = True

    if wp_removed:
        try:
            subprocess.run(["systemctl", "--user", "restart", "wireplumber"], capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Failed to restart WirePlumber: {e.stderr.strip()}")
        except Exception as e:
            print(f"Failed to execute systemctl: {e}")

    udev_file = Path("/etc/udev/rules.d/70-sound-blaster-g6.rules")
    if udev_file.exists():
        print("\n--- System Files Detected ---")
        print("To completely remove the hardware permissions (udev rules), run this command in your terminal:")
        print(f"    sudo rm -f {udev_file} && sudo udevadm control --reload-rules && sudo udevadm trigger")
        prompt_string("\nPress Enter to continue...", "")
    
    config_dir = Path.home() / ".config" / "g6_switcher"
    if config_dir.exists() and config_dir.is_dir():
        if prompt_bool("Do you also want to remove the configuration file?", False):
            shutil.rmtree(config_dir)
            print(f"Removed: {config_dir}")
        else:
            print(f"Preserved: {config_dir}")

    install_path = Path.home() / ".local" / "bin" / "g6_switcher"
    for suffix in ("", ".bak", ".tmp"):
        file_to_remove = install_path.with_suffix(suffix) if suffix else install_path
        if file_to_remove.exists():
            try:
                file_to_remove.unlink()
                print(f"Removed: {file_to_remove}")
            except Exception as e:
                print(f"Failed to remove {file_to_remove}: {e}")

    for lock_path in (Path(LOCK_FILENAME), Path("/tmp/sound_blaster_g6_switcher.lock")):
        if lock_path.exists():
            try:
                lock_path.unlink()
                print(f"Removed: {lock_path}")
            except Exception as e:
                print(f"Failed to remove lock file: {e}")

    print("\nUninstallation complete!")
    sys.exit(0)

def verify_service_status():
    print("Verifying background service status...")
    poll_deadline = time.monotonic() + 15
    is_stable = False
    last_status = "unknown"

    while time.monotonic() < poll_deadline:
        res = subprocess.run(["systemctl", "--user", "is-active", "g6_switcher.service"], capture_output=True, text=True)
        last_status = res.stdout.strip()

        if last_status == "active":
            # Stability pause to catch immediate post-startup crashes
            time.sleep(1.0)
            check = subprocess.run(["systemctl", "--user", "is-active", "g6_switcher.service"], capture_output=True, text=True).stdout.strip()
            if check == "active":
                is_stable = True
                break
        elif last_status in ("failed", "inactive"):
            # Fast-fail without waiting for full timeout
            break

        time.sleep(1.0)

    if not is_stable:
        journal_err = ""
        try:
            j_res = subprocess.run(
                ["journalctl", "--user", "-u", "g6_switcher.service", "-n", "5", "--no-pager"],
                capture_output=True, text=True, timeout=2
            )
            if j_res.stdout.strip():
                journal_err = f"\nRecent logs:\n{j_res.stdout.strip()}"
        except Exception:
            pass
        raise RuntimeError(f"Service status is '{last_status}'.{journal_err}")

def update_installation():
    print("=" * 60)
    print(" Sound BlasterX G6 Switcher Updater ")
    print("=" * 60)
    
    install_path = Path.home() / ".local" / "bin" / "g6_switcher"
    backup_path = install_path.with_suffix(".bak")
    temp_path = install_path.with_suffix(".tmp")
    
    if not install_path.exists():
        print("No existing installation found. Please run the script with '--setup' to install.")
        sys.exit(1)
    
    try:
        res = subprocess.run([sys.executable, str(install_path), "--version"], capture_output=True, text=True, check=True)
        # argparse version output format: "filename SCRIPT_VERSION"
        installed_version_str = res.stdout.strip().split()[-1]
        
        # Strip leading 'v'/'V' and trim suffixes (-beta, _pre, etc.) before comparison
        def parse_version(v_str):
            clean_str = re.split(r'[-_a-zA-Z]', v_str.lstrip('vV'))[0]
            return tuple(map(int, clean_str.split('.')))

        # Tuple comparison (e.g. (1.3) > (1.2))
        installed_tuple = parse_version(installed_version_str)
        current_tuple = parse_version(SCRIPT_VERSION)
        
        should_proceed = True
        if current_tuple == installed_tuple:
            print(f"Current version of Sound BlasterX G6 Switcher (v{SCRIPT_VERSION}) is already installed.")
            should_proceed = prompt_bool("Reinstall anyway?", False)
        elif current_tuple < installed_tuple:
            print(f"The installed version of Sound BlasterX G6 Switcher ({installed_version_str}) is newer than the version of this script (v{SCRIPT_VERSION}).")
            should_proceed = prompt_bool("Downgrade anyway? Downgrading the version may cause incompatibility with the current configuration.", False)
            
        if not should_proceed:
            print("Update cancelled.")
            sys.exit(0)
            
        print(f"Updating Sound BlasterX G6 Switcher from {installed_version_str} to v{SCRIPT_VERSION}...")
        current_script = Path(__file__).resolve()
        
        # Create a backup of the current binary before replacing
        shutil.copy2(install_path, backup_path)
        
        try:
            shutil.copy2(current_script, temp_path)
            temp_path.chmod(0o755)
            os.replace(temp_path, install_path)
            
            print("Restarting background service...")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            restart_res = subprocess.run(["systemctl", "--user", "restart", "g6_switcher.service"], capture_output=True, text=True)
            
            if restart_res.returncode != 0:
                err_detail = (restart_res.stderr or restart_res.stdout).strip()
                raise RuntimeError(f"systemctl restart failed: {err_detail}")

            verify_service_status()
                
            print(f"Update to version v{SCRIPT_VERSION} complete!")
            if backup_path.exists():
                backup_path.unlink()
            sys.exit(0)
            
        except Exception as e:
            print(f"\n[ERROR] Update failed: {e}")
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            if backup_path.exists():
                if prompt_bool("Do you want to rollback to the previous working version?", True):
                    os.replace(backup_path, install_path)
                    install_path.chmod(0o755)
                    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
                    subprocess.run(["systemctl", "--user", "restart", "g6_switcher.service"], capture_output=True)
                    print("Rollback complete.")
                else:
                    print(f"Backup preserved at: {backup_path}")
            sys.exit(1)
            
            
    except Exception as e:
        print(f"Failed to perform update: {e}")
        sys.exit(1)

def prompt_bool(question, default=True):
    valid = {"yes": True, "y": True, "ye": True, "no": False, "n": False}
    prompt = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            sys.stdout.write(question + prompt)
            choice = input().lower().strip()
            if not choice:
                return default
            if choice in valid:
                return valid[choice]
            sys.stdout.write("Please respond with 'yes' or 'no' (or 'y' or 'n').\n")
        except KeyboardInterrupt:
            print("\nInterrupted.")
            sys.exit(0)

def prompt_string(question, default):
    try:
        sys.stdout.write(f"{question} [{default}]: ")
        choice = input().strip()
        return choice if choice else default
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

def prompt_int(question, default):
    while True:
        try:
            sys.stdout.write(f"{question} [{default}]: ")
            choice = input().strip()
            if not choice:
                return default
            try:
                return int(choice)
            except ValueError:
                sys.stdout.write("Please enter a valid integer.\n")
        except KeyboardInterrupt:
            print("\nInterrupted.")
            sys.exit(0)

def prompt_offsets():
    default_offsets = {"95": 0.0, "100": 5.0}
    try:
        sys.stdout.write("Enter dynamic limiter offsets as 'vol:offset, vol:offset'\n[Default: 95:0.0, 100:5.0]:\n> ")
        choice = input().strip()
        if not choice:
            return default_offsets
        try:
            custom = {}
            for pair in choice.split(','):
                k, v = pair.split(':')
                custom[str(int(k.strip()))] = float(v.strip())
            return custom
        except Exception:
            sys.stdout.write("Invalid format. Using default offsets.\n")
            return default_offsets
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

def get_wireplumber_version():
    try:
        res = subprocess.run(["wireplumber", "--version"], capture_output=True, text=True, check=True)
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", res.stdout)
        if match:
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
    except Exception:
        pass
    return None

def setup_wireplumber(wp_version):
    print("\n--- Wireplumber Configuration ---")
    if not prompt_bool("Create a Wireplumber config to restrict the optical (S/PDIF) output to 48000Hz/24-bit?", True):
        return
        
    wp_major, wp_minor, _ = wp_version
    is_wp05 = (wp_major == 0 and wp_minor >= 5) or wp_major >= 1

    print(f"Creating config for Wireplumber {wp_major}.{wp_minor}")

    if is_wp05:
        wp_dir = Path.home() / ".config" / "wireplumber" / "wireplumber.conf.d"
        wp_file = wp_dir / "51-g6-optical.conf"
        wp_content = """monitor.alsa.rules = [
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
"""
    
    else:
        # Fallback for older WirePlumber 0.4.x which uses Lua scripts
        wp_dir = Path.home() / ".config" / "wireplumber" / "main.lua.d"
        wp_file = wp_dir / "51-g6-optical.lua"
        wp_content = """rule = {
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
"""
    
    wp_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(wp_file, 'w') as f:
            f.write(wp_content)
        print(f"Successfully created Wireplumber config: {wp_file}")
        subprocess.run(["systemctl", "--user", "restart", "wireplumber"], capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to restart WirePlumber: {e.stderr.strip()}")
    except Exception as e:
        print(f"Failed to setup Wireplumber config: {e}")

def setup_udev_rules():
    print("--- Hardware Permissions (Udev Rules) ---")
    # Priority set to 70 to execute before 73-seat-late.rules (required for systemd >= 258 uaccess tag handling)
    udev_file = Path("/etc/udev/rules.d/70-sound-blaster-g6.rules")
    
    g6_nodes = []
    try:
        for dev in os.listdir('/sys/class/hidraw'):
            try:
                with open(f'/sys/class/hidraw/{dev}/device/uevent', 'r') as f:
                    # Read once to prevent EOF on the second check
                    content = f.read().upper()
                    if '041E' in content and '3256' in content:
                        g6_nodes.append(f'/dev/{dev}')
            except Exception:
                # Ignore devices that cannot be read and continue the loop
                continue
    except Exception:
        pass

    # If nodes exist, verify if R/W access is already granted regardless of udev_file existence.
    # Otherwise, fallback to checking if the udev rule file is present.
    if g6_nodes:
        needs_rules = not all(os.access(node, os.R_OK | os.W_OK) for node in g6_nodes)
    else:
        needs_rules = not udev_file.exists()

    if not needs_rules:
        print("Udev rules and hidraw permissions are already correctly configured.")
        return True

    print("The script requires read/write access to the G6's hidraw devices.")
    
    if not prompt_bool("Create the necessary udev rules now?", True):
        print("Skipped. Warning: The script may fail to run due to PermissionError.")
        return False

    # Explicit ACTION!="remove" prevents missing tags on coldplug and change events in systemd >= 258
    rule_content = 'ACTION!="remove", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="041e", ATTRS{idProduct}=="3256", TAG+="uaccess"'
    cmd = f"echo '{rule_content}' | sudo tee {udev_file} > /dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger"
    
    print("\nPlease copy and run the following command in another terminal:")
    print("-" * 60)
    print(cmd)
    print("-" * 60)
    
    try:
        input("\nPress Enter once you have executed the command...")
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
    
    # Quick re-check to see if the user actually did it
    if udev_file.exists() or (g6_nodes and all(os.access(node, os.R_OK | os.W_OK) for node in g6_nodes)):
        print(f"Verified: hidraw permissions / udev rule {udev_file} configured properly.")
        return True
    else:
        print("Udev rule not found or permissions not applied yet. You may need to add it manually later.")
        return False

def generate_service_content(install_path):
    """Generate systemd user service unit definition."""
    return f"""[Unit]
Description=Sound BlasterX G6 Switcher
After=graphical-session.target pipewire.service pipewire-pulse.service pulseaudio.service
Wants=pipewire.service pipewire-pulse.service pulseaudio.service
# Stop restarting if it crashes 3 times within 60 seconds
StartLimitBurst=3
StartLimitIntervalSec=60

[Service]
Type=notify
NotifyAccess=main
Environment="PYTHONUNBUFFERED=1"
ExecStart={sys.executable} {install_path} --daemon
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
TimeoutStartSec=15

# Security Hardening
NoNewPrivileges=yes
LockPersonality=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native

[Install]
WantedBy=graphical-session.target
"""

def setup_autostart():
    print("\n--- Installation & Autostart ---")
    if not prompt_bool("Do you want to install this script and add it to autostart (systemd user service)?", True):
        return False, False
        
    # Copy the script to standard user bin directory
    bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    install_path = bin_dir / "g6_switcher"

    current_script = Path(__file__).resolve()
    
    if current_script != install_path:
        try:
            shutil.copy2(current_script, install_path)
            install_path.chmod(0o755)
            print(f"Script installed to: {install_path}")
        except Exception as e:
            print(f"Failed to copy script to {install_path}: {e}")
            return True, False
    else:
        print(f"Script is already located in {install_path}")
    
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = service_dir / "g6_switcher.service"
    
    service_content = generate_service_content(install_path)
    try:
        with open(service_file, 'w') as f:
            f.write(service_content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["systemctl", "--user", "enable", "g6_switcher.service"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        restart_res = subprocess.run(["systemctl", "--user", "restart", "g6_switcher.service"], capture_output=True, text=True)
        
        if restart_res.returncode != 0:
            err_detail = (restart_res.stderr or restart_res.stdout).strip()
            raise RuntimeError(f"systemctl restart failed: {err_detail}")
            
        verify_service_status()
        
        print(f"Successfully created and started systemd service: {service_file}")
        return True, True
    
    except Exception as e:
        print(f"Failed to setup autostart: {e}")
        return True, False

def run_setup_wizard(config_file, config_dir):
    print("=" * 60)
    print(" Sound BlasterX G6 Switcher Setup Wizard ")
    print("=" * 60)
    print("Press Enter to keep the default values shown in brackets [].\n")
    
    setup_udev_rules()
    
    log_lvl = prompt_string("\nEnter the logging level (INFO, DEBUG, WARNING, ERROR)", "INFO").upper()

    print("\n--- Presets Setup ---")
    print("\nThe script uses a strict priority order to detect EasyEffects or PulseEffects installations:")
    print("  1. EasyEffects (Flatpak)")
    print("  2. PulseEffects (Flatpak)")
    print("  3. EasyEffects (Native)")
    print("  4. PulseEffects (Native)")
    print("The first matching application found will be selected automatically.")
    print("Please keep this in mind if you have multiple versions installed in parallel.\n")
    
    print("\nNext the wizard will ask for the names of the output presets")
    print("that must already exist in EasyEffects or PulseEffects:")
    print("  - Headphones preset")
    print("  - Speakers preset")
    print("  - Default preset (recommended to be an empty preset without plugins,")
    print("    used to bypass effects when switching away from the G6)")
    print("Create and configure them manually in the application before continuing.\n")
    
    print("\nImportant: Do not bind these presets to the Sound BlasterX G6 or any other specific sound card.")
    print("Otherwise automatic switching will not work correctly.\n")

    hp_preset = prompt_string("Enter the preset name for HEADPHONES", "Headphones")
    spk_preset = prompt_string("Enter the preset name for SPEAKERS", "Speakers")
    def_preset = prompt_string("Enter the preset name for DEFAULT/BYPASS (used for non-G6 outputs)", "Default")
    
    print("\n--- Preset Tolerance ---")
    print("How should the script behave during output switching if you manually set a custom preset?")
    print("  disabled   - Always force automatic preset switching.")
    print("  once       - Skip automatic switching once.")
    print("  persistent - Suspend automatic switching until you manually re-select the headphones or speakers preset.")
    tol_mode = prompt_string("Enter mode (disabled, once, persistent)", "disabled").lower()
    if tol_mode not in ["disabled", "once", "persistent"]:
        print("Invalid input. Using 'disabled'.")
        tol_mode = "disabled"
    
    print("\n--- Headphones Volume Policy ---")
    save_hp = prompt_bool("Save and restore the previous headphones volume?", True)
    if save_hp:
        use_safe = False
        print("The script uses a fallback volume on initial launch, if the state file")
        print("is missing/corrupted, or in case of audio server query timeouts.")
        safe_vol = prompt_int("Enter a fallback volume percentage", 25)
    else:
        use_safe = prompt_bool("Always enforce a fixed safe volume when headphones are connected?", True)
        if use_safe:
            safe_vol = prompt_int("Enter safe volume percentage", 25)
        else:
            safe_vol = 25  # Internal safety fallback for pactl query timeouts
            print("Automatic volume adjustment disabled. System volume will remain untouched during switches.")
    
    print("\n--- Dynamic Limiter Setup ---")
    use_limiter = prompt_bool("Use a dynamic limiter for headphones to prevent clipping at high volume levels?", False)
    if use_limiter:
        print("\nThe Limiter filter must be present and enabled in the HEADPHONES preset in EasyEffects or PulseEffects.")
        print("Otherwise, this feature has no effect. The script only adjusts the limiter threshold;")
        print("it is unable to add or enable the filter automatically.\n")
        
        print("\nNote: The dynamic limiter works only on the preset assigned to HEADPHONES.")
        print("It is automatically disabled when switching to SPEAKERS (Line Out / S-PDIF) or any other preset.")
        print("It is also unavailable on EasyEffects v8.0.0-8.0.6.")
        print("Important: For the limiter to work correctly as clipping protection, you must disable 'Threshold Boost' in limiter settings.\n")
        
        limiter_offsets = prompt_offsets()
    else:
        limiter_offsets = {"100": 0.0}
        
    use_hw = prompt_bool("Enable automatic ALSA hardware profile switching? (allows using the optical (S/PDIF) output for speakers while keeping Direct Mode for headphones)", False)
    if use_hw:
        if shutil.which("wireplumber"):
            wp_version = get_wireplumber_version()
            if wp_version:
                setup_wireplumber(wp_version)
            else:
                print("WirePlumber version check failed (returned invalid data or crashed). 'use_hardware_profiles' automatically disabled.")
                use_hw = False
        else:
            print("Hardware profile switching requires WirePlumber to handle dynamic sample rates. 'use_hardware_profiles' automatically disabled.")
            use_hw = False
        
    dsp_warn = prompt_bool("Show a notification when the script detects that the Sound BlasterX G6 has booted in hardware DSP mode?", False)
    
    config = {
        "log_level": log_lvl,
        "presets": {
            "headphones": hp_preset,
            "speakers": spk_preset,
            "default": def_preset
        },
        "features": {
            "preset_tolerance_mode": tol_mode,
            "use_safe_volume": use_safe,
            "safe_volume_percent": safe_vol,
            "save_headphones_volume": save_hp,
            "use_dynamic_limiter": use_limiter,
            "use_hardware_profiles": use_hw,
            "warn_on_dsp_boot": dsp_warn
        },
        "dynamic_limiter_offsets": limiter_offsets
    }
    
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
        
    print(f"\nConfiguration saved to {config_file}")
    
    wants_service, service_started = setup_autostart()
    print("\nSetup complete!")

    if wants_service:
        if service_started:
            print("Background service has been successfully started via systemd.")
        else:
            print("Background service installation was requested, but failed to start.")
            print("Please check service logs (journalctl --user -xeu g6_switcher.service) for details.")
        print("Exiting interactive setup mode...")
        sys.exit(0 if service_started else 1)

    return config

# === PATHS & LOCKING ===
CONFIG_DIR = Path.home() / ".config" / "g6_switcher"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
LOCK_FILENAME = str(RUNTIME_DIR / "sound_blaster_g6_switcher.lock") if RUNTIME_DIR.exists() else "/tmp/sound_blaster_g6_switcher.lock"

if args.uninstall:
    try:
        uninstall()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

if args.update:
    try:
        update_installation()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

if args.setup:
    try:
        run_setup_wizard(CONFIG_FILE, CONFIG_DIR)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)
    sys.exit(0)

lock_file = open(LOCK_FILENAME, 'w')
try:
    # Attempt to acquire an exclusive non-blocking lock
    fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    app_lock = lock_file  # Keep file handle alive to preserve the lock
except IOError:
    error_msg = "Another instance of the script is already running. Exiting.\nRun with '-h' for help."
    print(error_msg)
    if args.daemon:
        send_notification(f"Startup Error:\n{error_msg}", "dialog-error")
    lock_file.close()
    sys.exit(1)

# === DEFAULT CONFIGURATION ===
DEFAULT_CONFIG = {
    "log_level": "INFO",
    "presets": {
        "headphones": "Headphones",
        "speakers": "Speakers",
        "default": "Default"
    },
    "features": {
        "preset_tolerance_mode": "disabled",
        "use_safe_volume": True,
        "safe_volume_percent": 25,
        "save_headphones_volume": True,
        "use_dynamic_limiter": False,
        "use_hardware_profiles": False,
        "warn_on_dsp_boot": False
    },
    "dynamic_limiter_offsets": {
        "95": 0.0,
        "100": 5.0
    }
}

# === CONFIGURATION & STATE MANAGEMENT ===
def validate_and_fix_config(config_data, default_data):
    errors = []
    if not isinstance(config_data, dict):
        return default_data.copy(), ["Configuration file is structurally invalid (not a dictionary)."]

    # 1. Logging Level
    valid_levels = ["INFO", "DEBUG", "WARNING", "ERROR"]
    if config_data.get("log_level") not in valid_levels:
        errors.append(f"'log_level' is invalid (must be one of {valid_levels}).")
        config_data["log_level"] = default_data["log_level"]

    # 2. Presets
    presets = config_data.get("presets")
    if not isinstance(presets, dict):
        errors.append("'presets' section is missing or invalid.")
        config_data["presets"] = default_data["presets"].copy()
    else:
        for key in ["headphones", "speakers", "default"]:
            if not isinstance(presets.get(key), str) or not presets[key].strip():
                errors.append(f"'presets.{key}' is invalid or empty.")
                presets[key] = default_data["presets"][key]

    # 3. Features
    features = config_data.get("features")
    if not isinstance(features, dict):
        errors.append("'features' section is missing or invalid.")
        config_data["features"] = default_data["features"].copy()
    else:
        bool_features = [
            "use_safe_volume", "save_headphones_volume", 
            "use_dynamic_limiter", "use_hardware_profiles", "warn_on_dsp_boot"
        ]
        for key in bool_features:
            if not isinstance(features.get(key), bool):
                errors.append(f"'features.{key}' must be a boolean.")
                features[key] = default_data["features"][key]

        tol_mode = features.get("preset_tolerance_mode")
        if tol_mode not in ["disabled", "once", "persistent"]:
            errors.append("'features.preset_tolerance_mode' must be 'disabled', 'once', or 'persistent'.")
            features["preset_tolerance_mode"] = default_data["features"]["preset_tolerance_mode"]
        
        vol = features.get("safe_volume_percent")
        if type(vol) is not int or not (0 <= vol <= 100):
            errors.append("'features.safe_volume_percent' must be an integer between 0 and 100.")
            features["safe_volume_percent"] = default_data["features"]["safe_volume_percent"]

    # 4. Dynamic Limiter Offsets
    offsets = config_data.get("dynamic_limiter_offsets")
    if not isinstance(offsets, dict):
        errors.append("'dynamic_limiter_offsets' section is missing or invalid.")
        config_data["dynamic_limiter_offsets"] = default_data["dynamic_limiter_offsets"].copy()
    else:
        valid_offsets = {}
        offset_error = False
        for k, v in offsets.items():
            try:
                vol = int(k)
                offset = float(v)
                
                # Semantic validation: Volume between 0 and 150%, Offset must be finite
                if not (0 <= vol <= 150):
                    raise ValueError("Volume out of logical bounds")
                # Check for Infinity or NaN
                if offset == float('inf') or offset == float('-inf') or offset != offset:
                    raise ValueError("Offset must be a finite number")
                    
                valid_offsets[str(vol)] = offset
            except (ValueError, TypeError):
                offset_error = True
        
        if offset_error or not valid_offsets:
            if offset_error:
                errors.append("'dynamic_limiter_offsets' contains invalid or out-of-bounds values.")
            else:
                errors.append("'dynamic_limiter_offsets' cannot be empty.")
            config_data["dynamic_limiter_offsets"] = default_data["dynamic_limiter_offsets"].copy()
        else:
            config_data["dynamic_limiter_offsets"] = valid_offsets

    return config_data, errors

config_errors = []

def load_json(file_path, default_data, is_main_config=False):
    global config_errors
    if not file_path.exists():
        if is_main_config:
            sd_notify("STATUS=Startup error: Configuration file not found\n")
            if args.daemon:
                send_notification(f"Startup Error:\nConfiguration file not found! Please run the script with parameter '--setup'.", "dialog-error")
                sys.exit(1)
            else:
                print("Configuration file not found.\n")
                if prompt_bool("Do you want to run the initial setup wizard now?", True):
                    return run_setup_wizard(file_path, file_path.parent)
                else:
                    print("Setup aborted.")
                    print("Please configure the script manually or run it with '--setup'.")
                    print("Exiting.")
                    sys.exit(0)
        else:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(default_data, f, indent=4)
            return default_data.copy()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        if is_main_config:
            data, config_errors = validate_and_fix_config(data, default_data)
        return data
    except json.JSONDecodeError:
        if is_main_config:
            config_errors.append("Failed to parse JSON file (syntax error).")
        return default_data.copy()
    except Exception as e:
        if is_main_config:
            config_errors.append(f"Failed to read JSON file: {e}")
        return default_data.copy()

def save_state():
    if not config["features"]["save_headphones_volume"]:
        return
    state_data = {"last_headphones_volume": last_headphones_volume}
    tmp_file = STATE_FILE.with_suffix('.tmp')
    try:
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(state_data, f, indent=4)
        
        # Atomic replacement to prevent corruption on unexpected power loss
        os.replace(tmp_file, STATE_FILE)
        logger.debug(f"State saved: {state_data}")
    except Exception as e:
        logger.error(f"Failed to save state file atomically: {e}")
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass

# Load Configuration and State
config = load_json(CONFIG_FILE, DEFAULT_CONFIG, is_main_config=True)
state = load_json(STATE_FILE, {"last_headphones_volume": config["features"]["safe_volume_percent"]})

# === LOGGING SETUP ===
numeric_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)

log_format = '[%(levelname)s] %(message)s' if args.daemon else '%(asctime)s [%(levelname)s] %(message)s'

logging.basicConfig(
    level=numeric_level,
    format=log_format,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("G6_Switcher")

def abort_startup(error_msg):
    """Log critical startup error, notify user in daemon mode, and exit."""
    logger.critical(error_msg)
    sd_notify(f"STATUS=Startup error: {error_msg}\n")
    if args.daemon:
        send_notification(f"Startup Error:\n{error_msg}", "dialog-error")
    sys.exit(1)

if config_errors:
    error_list = "\n".join([f"  - {err}" for err in config_errors])
    logger.warning(
        f"Configuration validation failed. Invalid values were ignored and replaced with defaults for this session:\n"
        f"{error_list}\n"
        f"Please correct the configuration file ({CONFIG_FILE}) manually or run the script with parameter '--setup'."
    )   
    if args.daemon:
        send_notification("Configuration error detected!\nCheck service logs (journalctl --user -xeu g6_switcher.service) for details.", "dialog-warning")

# === GLOBAL VARIABLES ===
PRESET_HEADPHONES = config["presets"]["headphones"]
PRESET_SPEAKERS = config["presets"]["speakers"]
PRESET_DEFAULT = config["presets"]["default"]

TOLERANCE_MODE = config["features"]["preset_tolerance_mode"]
USE_SAFE_VOL = config["features"]["use_safe_volume"]
SAFE_VOL = config["features"]["safe_volume_percent"]
SAVE_HP_VOL = config["features"]["save_headphones_volume"]
USE_LIMITER = config["features"]["use_dynamic_limiter"]
USE_HW_PROFILES = config["features"]["use_hardware_profiles"]
WARN_ON_DSP_BOOT = config["features"]["warn_on_dsp_boot"]
VOL_OFFSETS = {int(k): float(v) for k, v in config["dynamic_limiter_offsets"].items()}

last_speaker_volume = SAFE_VOL if USE_SAFE_VOL else 50

# Load and validate saved headphones volume
saved_vol = state.get("last_headphones_volume") if isinstance(state, dict) else None
if isinstance(saved_vol, int) and (0 <= saved_vol <= 150):
    last_headphones_volume = saved_vol
else:
    logger.warning(f"Invalid saved state volume ({saved_vol}). Resetting to safe volume.")
    last_headphones_volume = SAFE_VOL

if not SAVE_HP_VOL and USE_SAFE_VOL:
    last_headphones_volume = SAFE_VOL

first_run = True
force_next_preset = False
current_preset = ""
preset_applied_by_script = ""
current_mode = "UNKNOWN"    # Tracks current active output
last_applied_vol = -1       # Prevents gsettings spam for identical volumes
last_seen_vol = -1
vol_applied_by_script = -1
last_process_time = 0.0
g6_sink_name = "Sound_BlasterX_G6"
g6_active = False
last_default_sink = None
active_hid_fds = {}
sel = selectors.DefaultSelector()
g6_alsa_dev = None
g6_pa_card = None
wireplumber_restart_pending = False

# === ENGINE AUTO-DETECTION & VALIDATION (EasyEffects, PulseEffects, pactl, gsettings) ===
flatpak_available = False
flatpak_used = False
pipewire_active = False
wireplumber_available = False
APP_NAME = None
ENGINE_ACTIVE = False
EE_PRESET_KEY_717 = False
EE_IS_QT = False
EE_SERVER_SUPPORTED = False
EE_PRESETS_FORMAT_804 = False
EE_SERVER_SOCKET_PATH = None

if not shutil.which("pactl"):
    abort_startup("Required utility 'pactl' not found! Install 'pipewire-pulse' or 'pulseaudio-utils'.")

if shutil.which("flatpak"):
    flatpak_available = True

if flatpak_available:
    if subprocess.run(["flatpak", "info", "com.github.wwmm.easyeffects"], capture_output=True).returncode == 0:
        APP_NAME = "EasyEffects"
        APP_NAME_CMD = ["flatpak", "run", "com.github.wwmm.easyeffects"]
        SCHEMA_BASE = "com.github.wwmm.easyeffects"
        LIMITER_PATH = f"{SCHEMA_BASE}.limiter:/com/github/wwmm/easyeffects/streamoutputs/limiter/0/"
        LIMITER_KEY = "threshold"
        ENGINE_INFO = "EasyEffects (PipeWire, Flatpak)"
    elif subprocess.run(["flatpak", "info", "com.github.wwmm.pulseeffects"], capture_output=True).returncode == 0:
        APP_NAME = "PulseEffects"
        APP_NAME_CMD = ["flatpak", "run", "com.github.wwmm.pulseeffects"]
        SCHEMA_BASE = "com.github.wwmm.pulseeffects"
        LIMITER_PATH = f"{SCHEMA_BASE}.limiter:/com/github/wwmm/pulseeffects/sinkinputs/limiter/"
        LIMITER_KEY = "limit"
        ENGINE_INFO = "PulseEffects (PulseAudio, Flatpak)"

if APP_NAME is None:
    if shutil.which("easyeffects"):
        APP_NAME = "EasyEffects"
        APP_NAME_CMD = ["easyeffects"]
        SCHEMA_BASE = "com.github.wwmm.easyeffects"
        LIMITER_PATH = f"{SCHEMA_BASE}.limiter:/com/github/wwmm/easyeffects/streamoutputs/limiter/0/"
        LIMITER_KEY = "threshold"
        ENGINE_INFO = "EasyEffects (PipeWire)"
    elif shutil.which("pulseeffects"):
        APP_NAME = "PulseEffects"
        APP_NAME_CMD = ["pulseeffects"]
        SCHEMA_BASE = "com.github.wwmm.pulseeffects"
        LIMITER_PATH = f"{SCHEMA_BASE}.limiter:/com/github/wwmm/pulseeffects/sinkinputs/limiter/"
        LIMITER_KEY = "limit"
        ENGINE_INFO = "PulseEffects (PulseAudio)"
    else:
        abort_startup("Required application EasyEffects or PulseEffects not found!")

if APP_NAME_CMD[0] == "flatpak":
    flatpak_used = True

if not shutil.which("amixer") and USE_HW_PROFILES:
    abort_startup("Required utility 'amixer' not found! Ensure 'alsa-utils' is installed or switch 'use_hardware_profiles' to 'false'.")
    
# Check for PipeWire & WirePlumber
try:
    # Check if PipeWire is the active sound server
    pactl_info = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5)
    if "PipeWire" in pactl_info.stdout:
        pipewire_active = True
            
    # Check if WirePlumber service exists
    wp_status = subprocess.run(["systemctl", "--user", "status", "wireplumber"], capture_output=True, text=True)
    if wp_status.returncode in (0, 3): # 0 = active, 3 = inactive (but exists)
        wireplumber_available = True
            
except Exception as e:
    logger.error(f"Failed to verify PipeWire/WirePlumber status: {e}")

if USE_HW_PROFILES:
    if not pipewire_active or not wireplumber_available:
        logger.warning("Hardware profile switching requires PipeWire and WirePlumber to handle dynamic sample rates. 'use_hardware_profiles' automatically disabled.")
        USE_HW_PROFILES = False

# Check if APP_NAME process is running
pgrep_available = False
if shutil.which("pgrep"):
    pgrep_available = True
    
def is_engine_running():
    if flatpak_used:
        result = subprocess.run(["flatpak", "ps"], capture_output=True, text=True, check=False)       
        return SCHEMA_BASE in result.stdout

    if pgrep_available:
        result = subprocess.run(["pgrep", "-x"] + APP_NAME_CMD, capture_output=True, check=False)
        return result.returncode == 0

    # Fallback path if pgrep is unavailable
    result = subprocess.run(["ps", "-e"], capture_output=True, text=True, check=False)
    return APP_NAME_CMD[0] in result.stdout


ENGINE_ACTIVE = is_engine_running()

if not ENGINE_ACTIVE:
    if args.daemon:
        logger.info(f"Waiting for {APP_NAME} to autostart...")
        
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if is_engine_running():
                ENGINE_ACTIVE = True
                logger.info(f"Background service for {APP_NAME} detected.")
                break
            time.sleep(1)
        
        if not ENGINE_ACTIVE:
            logger.info(f"Autostart timeout reached. Attempting to force starting {APP_NAME}...")
    
    # If not running in daemon mode, or if autostart timeout expired
    if not ENGINE_ACTIVE:
        if args.daemon:
            subprocess.run(
                ["systemd-run", "--user", "--collect", f"--unit=g6-switcher-{APP_NAME.lower()}-service",
                f"--description={APP_NAME} Service for Sound BlasterX G6 Switcher",
                *APP_NAME_CMD, "--gapplication-service"],
                check=False, capture_output=True, timeout=5
            )
        else:
            logger.info(f"Background service for {APP_NAME} is not running. Attempting to start...")
            subprocess.Popen(
                APP_NAME_CMD + ["--gapplication-service"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True
            )
    
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if is_engine_running():
                ENGINE_ACTIVE = True
                logger.info(f"Background service for {APP_NAME} successfully started.")
                break
            time.sleep(1)
        
        if not ENGINE_ACTIVE:
            abort_startup(f"Failed to start {APP_NAME} in background service mode (startup timeout).")
            
# Version Check (GTK (< 8.0.0) vs Qt (>= 8.0.0) and Qt with improved Local Server (>= 8.0.7)), 
if APP_NAME == "EasyEffects":
    try:
        ver_result = subprocess.run(
            APP_NAME_CMD + ["--version"], 
            capture_output=True, text=True, timeout=2
        )
        if ver_result.returncode == 0: # Older versions (< 7.1.4) fail with returncode != 0 and print error messages
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", ver_result.stdout)
            if match:
                major, minor, patch = map(int, match.groups())
                logger.debug(f"{ENGINE_INFO} v{major}.{minor}.{patch} detected.")
                if major < 8 and (major, minor, patch) >= (7, 1, 7):
                    EE_PRESET_KEY_717 = True
                if major >= 8:
                    EE_IS_QT = True
                    if (major, minor, patch) >= (8, 0, 4):
                        EE_PRESETS_FORMAT_804 = True
                    # LocalServer functionality for status/limiter parameters implemented in version >= 8.0.7
                    if (major, minor, patch) >= (8, 0, 7):
                        EE_SERVER_SUPPORTED = True
    except Exception as e:
        # silently fallback to legacy GTK/gsettings logic
        logger.debug(f"Version check failed or unsupported, falling back to GTK: {e}")

if not EE_IS_QT: #gsettings validation for gtk only
    PRESET_KEY = ("last-loaded-output-preset" if EE_PRESET_KEY_717 else "last-used-output-preset")

    if not shutil.which("gsettings"):
        abort_startup("Required utility 'gsettings' not found! Ensure 'glib2' is installed.")

    try:
        if flatpak_used:
            result = subprocess.run(
                ["flatpak", "run", f"--command=gsettings", SCHEMA_BASE, "list-keys", SCHEMA_BASE],
                capture_output=True, text=True
            )
        else:
            result = subprocess.run(
                ["gsettings", "list-keys", SCHEMA_BASE],
                capture_output=True, text=True
            )
        if result.returncode != 0:
            raise Exception("Schema missing")
    except Exception:
        abort_startup(f"GSettings schema '{SCHEMA_BASE}' not found! Check {APP_NAME} installation.")
else:
    # Version 8.0.0+ moves presets and profiles to XDG_DATA_HOME (~/.local/share)
    # and keeps database files in XDG_CONFIG_HOME (~/.config).
    if flatpak_used:
        EE_QT_DATA_DIR = Path.home() / ".var" / "app" / "com.github.wwmm.easyeffects" / "data" / "easyeffects" / "output"
    else:
        EE_QT_DATA_DIR = Path.home() / ".local" / "share" / "easyeffects" / "output"

    # Verify that the new Qt data path exists (created/migrated by v8.0.0+)
    if not EE_QT_DATA_DIR.exists():
        abort_startup(f"Qt data directory not found at {EE_QT_DATA_DIR}. Check EasyEffects installation.")
        
    # Automatically disable dynamic limiter for versions 8.0.0–8.0.6
    if not EE_SERVER_SUPPORTED and USE_LIMITER:
        USE_LIMITER = False
        logger.warning("EasyEffects 8.0.0–8.0.6 lacks LocalServer functionality. 'use_dynamic_limiter' automatically disabled.")
    
    elif USE_LIMITER:
        if inotify_init1 == None or inotify_add_watch == None:
            USE_LIMITER = False
            logger.warning("Preset tracking for EasyEffects 8.0.0+ requires inotify syscalls. 'use_dynamic_limiter' is automatically disabled.")

# Localserver Discovery (For EE >= 8.0.0)
if EE_IS_QT:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    
    # Prioritize socket paths based on environment
    if flatpak_used:
        candidates = [
            f"{xdg_runtime}/.flatpak/com.github.wwmm.easyeffects/xdg-run/EasyEffectsServer",
            f"{xdg_runtime}/.flatpak/com.github.wwmm.easyeffects/tmp/EasyEffectsServer",
            f"{xdg_runtime}/app/com.github.wwmm.easyeffects/xdg-run/EasyEffectsServer"
        ]
    else:
        candidates = [f"{xdg_runtime}/EasyEffectsServer"]
        
    candidates.append("/tmp/EasyEffectsServer")

    for socket_path in candidates:
        if not os.path.exists(socket_path):
            continue
            
        # Prevent Flatpak/Native cross-talk for the shared /tmp socket
        if socket_path == "/tmp/EasyEffectsServer":
            socket_belongs_to_flatpak = None  # None = unknown / ownership cannot be guaranteed
            try:
                with open("/proc/net/unix", "r") as proc_net_unix:
                    inode = None
                    for line in proc_net_unix:
                        parts = line.split()
                        if parts and parts[-1] == socket_path:
                            inode = parts[6] if len(parts) >= 7 else None
                            break
                    
                if inode:
                    target_link = f"socket:[{inode}]"
                    # Search through running processes to find the socket owner
                    for pid_dir in (d for d in os.listdir("/proc") if d.isdigit()):
                        fd_dir = f"/proc/{pid_dir}/fd"
                        try:
                            # Check if any file descriptor points to our socket's inode
                            if any(os.readlink(f"{fd_dir}/{fd}") == target_link for fd in os.listdir(fd_dir)):
                                socket_belongs_to_flatpak = os.path.exists(f"/proc/{pid_dir}/root/.flatpak-info")
                                break
                        except OSError:
                            # Process died while we were reading its fds, or access denied. Safe to skip.
                            continue
                            
                if socket_belongs_to_flatpak is None or socket_belongs_to_flatpak != flatpak_used:
                    if socket_belongs_to_flatpak is not None:
                        logger.warning(
                            f"Conflicting EasyEffects instances detected at '{socket_path}'. "
                            f"Expected Flatpak: {flatpak_used}, but found Flatpak: {socket_belongs_to_flatpak}. "
                            "Please check your installation to prevent cross-talk."
                        )
                    else:
                        logger.debug(f"Could not verify ownership for '{socket_path}'. Skipping to prevent cross-talk.")
                    continue
                    
            except Exception as e:
                logger.debug(f"Failed to determine owner of '{socket_path}': {e}")
                continue

        # Verify the socket is alive and accepts connections
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(socket_path)
            EE_SERVER_SOCKET_PATH = socket_path
            logger.debug(f"Successfully connected to EasyEffects LocalServer socket at '{socket_path}'.")
            break
        except Exception as e:
            logger.debug(f"Dead or inaccessible socket found at '{socket_path}': {e}")
            continue

    if not EE_SERVER_SOCKET_PATH:
        logger.warning("LocalServer socket not found or unresponsive.")
        EE_SERVER_SUPPORTED = False
        if USE_LIMITER:
            logger.warning("'use_dynamic_limiter' automatically disabled.")
            USE_LIMITER = False

def get_available_presets():
    """Fetch list of available output presets from EasyEffects or PulseEffects."""
    try:
        result = subprocess.run(
            APP_NAME_CMD + ["--presets"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C"}
        )
        if result.returncode != 0:
            return None

        available_presets = []
        if EE_PRESETS_FORMAT_804:
            # Parse new format (8.0.4+): Read lines after "Output presets:" until an empty line
            in_output_section = False
            for line in result.stdout.splitlines():
                line_stripped = line.strip()
                
                if line_stripped.startswith("Output presets:"):
                    in_output_section = True
                    continue
                elif line_stripped.startswith("Input presets:") or line_stripped.startswith("No input presets.") or not line_stripped:
                    if in_output_section: # Stop parsing if we hit an empty line or input section
                        break
                
                if in_output_section:
                    parts = line.split('\t')
                    if len(parts) >= 2 and parts[0].isdigit():
                        available_presets.append(parts[1].strip())
        else:
            # Parse old format (pre-8.0.4): Comma-separated list on a single line
            for line in result.stdout.splitlines():
                if line.startswith("Output Presets:"):
                    presets_raw = line.split("Output Presets:", 1)[1]
                    # Split by comma, strip whitespace, and ignore empty strings
                    available_presets = [p.strip() for p in presets_raw.split(',') if p.strip()]
                    break
        
        return available_presets
    except Exception as e:
        if "logger" in globals():
            logger.warning(f"Failed to fetch presets from {APP_NAME}: {e}")
        return None

# Validate required presets
presets = get_available_presets()
if presets is None:
    abort_startup(f"Unable to verify presets in {APP_NAME}!")

for preset in [PRESET_HEADPHONES, PRESET_SPEAKERS, PRESET_DEFAULT]:
    if preset not in presets:
        abort_startup(f"Output preset '{preset}' is not configured in {APP_NAME}!")

# === CORE FUNCTIONS ===
def apply_hardware_profile(mode):
    """Switch ALSA clock source and card profile."""
    global wireplumber_restart_pending
    
    if not USE_HW_PROFILES or not g6_alsa_dev or not g6_pa_card:
        return

    try:
        if mode == "SPEAKERS":
            subprocess.run(["amixer", "-c", g6_alsa_dev, "sset", "Speaker Clock Source", "DSP Clock"], check=True, capture_output=True, timeout=1)
            subprocess.run(["pactl", "set-card-profile", g6_pa_card, "output:iec958-stereo"], check=True, capture_output=True, timeout=1)
            logger.info("Applied hardware profile for SPEAKERS.")
        elif mode == "HEADPHONES":
            subprocess.run(["amixer", "-c", g6_alsa_dev, "sset", "Speaker Clock Source", "Stereo Direct"], check=True, capture_output=True, timeout=1)
            subprocess.run(["pactl", "set-card-profile", g6_pa_card, "output:analog-stereo"], check=True, capture_output=True, timeout=1)
            logger.info("Applied hardware profile for HEADPHONES.")
        time.sleep(0.1)
    
    except subprocess.TimeoutExpired as e:
        logger.error(f"Hardware profile switch timed out for {mode}: {e}")
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode().strip() if e.stderr else str(e)
        logger.error(f"Failed to apply hardware profile ({mode}): {err_msg}")

def calculate_offset(vol):
    """Calculate limiter threshold offset based on current volume"""
    points = sorted(VOL_OFFSETS.keys())
    if not points:
        return 0.0
    if vol <= points[0]: return 0.0
    if vol >= points[-1]: return VOL_OFFSETS[points[-1]]

    # Linear interpolation between points
    for i in range(len(points) - 1):
        v1, v2 = points[i], points[i+1]
        if v1 <= vol <= v2:
            o1, o2 = VOL_OFFSETS[v1], VOL_OFFSETS[v2]
            ratio = (vol - v1) / (v2 - v1)
            offset = o1 + (o2 - o1) * ratio
            return round(offset + 0.01, 1) # Bias upward before rounding
    return 0.0

def get_current_volume_int(target_sink="@DEFAULT_SINK@"):
    """Fetch current system volume as an integer"""
    try:
        result = subprocess.run(
            ["pactl", "get-sink-volume", target_sink],
            capture_output=True, text=True, timeout=1
        )
        match = re.search(r"(\d+)%", result.stdout)
        if match:
            return int(match.group(1))
    except subprocess.TimeoutExpired:
        logger.error("Timeout while fetching volume from pactl")
    except Exception as e:
        logger.error(f"Failed to fetch volume: {e}")
    return SAFE_VOL

def get_current_preset_name():
    """Retrieve the currently active EasyEffects/PulseEffects output preset"""
    if EE_IS_QT:
        if EE_SERVER_SUPPORTED and EE_SERVER_SOCKET_PATH:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect(EE_SERVER_SOCKET_PATH)
                    s.sendall(b"get_last_loaded_preset:output\n")
                    response = s.recv(1024).decode("utf-8").strip()
                    if response:
                        return response
            except socket.timeout:
                logger.warning("Timeout while reading current preset name via LocalServer.")
            except Exception as e:
                logger.warning(f"Failed to fetch Qt preset via LocalServer: {e}")
            return ""
        
        # EasyEffects 8.0.0–8.0.6 LocalServer lacks get_last_loaded_preset support, use built-in CLI to fetch active preset
        try:
            result = subprocess.run(APP_NAME_CMD + ["-a", "output"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if lines:
                    return lines[-1]
        except subprocess.TimeoutExpired:
            logger.warning("Timeout while reading current preset name via CLI.")
        except Exception as e:
            logger.warning(f"Failed to fetch Qt preset via CLI: {e}")
        return ""
    
    # Legacy GTK backend (gsettings)
    try:
        if flatpak_used:
            cmd = ["flatpak", "run", "--command=gsettings", SCHEMA_BASE, "get", SCHEMA_BASE, PRESET_KEY]
        else:
            cmd = ["gsettings", "get", SCHEMA_BASE, PRESET_KEY]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        return result.stdout.strip().strip("'")
    except subprocess.TimeoutExpired:
        logger.warning("Timeout while reading current preset name.")
    except Exception as e:
        logger.warning(f"Failed to fetch current preset name via gsettings: {e}")
    return ""

def apply_dynamic_processing(vol):
    """Dynamically adjust limiter threshold based on volume"""
    global last_process_time, last_applied_vol

    if not USE_LIMITER or vol == last_applied_vol:
        return

    now = time.time()
    if now - last_process_time < 0.05:  # debounce 50ms
        return
    last_process_time = now

    offset = -calculate_offset(vol)
    if EE_IS_QT:
        if EE_SERVER_SUPPORTED and EE_SERVER_SOCKET_PATH:
            try:
                # Use a context manager to ensure the socket closes automatically
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    s.connect(EE_SERVER_SOCKET_PATH)
                    command = f"set_property:output:limiter:0:threshold:{offset}\n"
                    s.sendall(command.encode('utf-8'))
                    logger.debug(f"Volume: {vol}% | Limiter threshold offset: {offset}dB")
                    last_applied_vol = vol
            except socket.timeout:
                logger.error("Timeout applying limiter settings via LocalServer.")
            except Exception as e:
                logger.error(f"Error applying limiter settings via LocalServer: {e}")
    else:
        try:
            # Apply settings via gsettings on-the-fly
            if flatpak_used:
                subprocess.run(
                    ["flatpak", "run", "--command=gsettings", SCHEMA_BASE, "set", LIMITER_PATH, LIMITER_KEY, str(offset)],
                    capture_output=True, text=True, timeout=1
                )
            else:
                subprocess.run(
                    ["gsettings", "set", LIMITER_PATH, LIMITER_KEY, str(offset)],
                    capture_output=True, text=True, timeout=1
                ) 
            logger.debug(f"Volume: {vol}% | Limiter threshold offset: {offset}dB")
            last_applied_vol = vol
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout while applying limiter offset via gsettings.")
        except Exception as e:
            logger.error(f"Error applying limiter settings via gsettings: {e}")

def set_volume(level):
    """Set system volume (expects integer level)"""
    global vol_applied_by_script
    logger.info(f"Setting volume to: {level}%")
    try:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"], check=True, timeout=1)
        vol_applied_by_script = level
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while setting volume")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to set volume: {e}")
    except Exception as e:
        logger.error(f"Unexpected error while setting volume: {e}")

def set_preset(name):
    """Load a specific output preset in the audio engine"""
    global current_preset, preset_applied_by_script
    
    icon_name = ""
    if name == PRESET_HEADPHONES:
        icon_name = "audio-headphones"
    elif name == PRESET_SPEAKERS:
        icon_name = "audio-speakers"
    
    if EE_IS_QT and EE_SERVER_SOCKET_PATH:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(EE_SERVER_SOCKET_PATH)

                if EE_SERVER_SUPPORTED:
                    command = f"load_preset:output:{name}\n"
                else:
                    # EasyEffects 8.0.0–8.0.6
                    command = f"load_preset:1:{name}\n"

                s.sendall(command.encode("utf-8"))
                current_preset = name
                preset_applied_by_script = name
                logger.info(f"Applied {APP_NAME} preset '{name}'.")
                send_notification(f"Applied preset: '{name}'.", icon_name)
                return
        except socket.timeout:
            logger.error(f"Timeout loading preset '{name}' via LocalServer.")
            return
        except Exception as e:
            logger.error(f"Error loading preset '{name}' via LocalServer: {e}")
            return
    
    # EasyEffects < 8.0.4 does not propagate preset-load failures through
    # the CLI exit status, so successful subprocess completion cannot
    # be treated as proof that the preset was actually applied.
    try:
        result = subprocess.run(APP_NAME_CMD + ["--load-preset", name], capture_output=True, text=True, timeout=1)
        
        # EasyEffects < 7.1.7 does not update last-used-output-preset
        # when a preset is loaded through the CLI, so keep GSettings
        # state synchronized manually.
        if not EE_IS_QT and not EE_PRESET_KEY_717:
            # Sync gsettings state to reflect the preset change
            try:
                if flatpak_used:
                    subprocess.run(
                        ["flatpak", "run", "--command=gsettings", SCHEMA_BASE, "set", SCHEMA_BASE, PRESET_KEY, f"'{name}'"],
                        timeout=1, capture_output=True
                    )
                else:
                    subprocess.run(
                        ["gsettings", "set", SCHEMA_BASE, PRESET_KEY, f"'{name}'"],
                        timeout=1, capture_output=True
                    )
            except subprocess.TimeoutExpired:
                logger.warning(f"Timeout while syncing gsettings preset to '{name}'")
            except Exception as e:
                logger.warning(f"Failed to sync gsettings preset: {e}")
        if result.returncode in (0, 1):
            current_preset = name
            preset_applied_by_script = name
            logger.info(f"Applied {APP_NAME} preset '{name}'.")
            send_notification(f"Applied preset: '{name}'.", icon_name)
            return
        else:
            logger.error(f"{APP_NAME} error (code {result.returncode}): {result.stderr}")
            return
    except subprocess.TimeoutExpired:
        logger.error(f"Timeout while loading preset '{name}'")
    except Exception as e:
        logger.error(f"Failed to apply preset: {e}")

def check_preset_tolerance():
    """Evaluate if preset switching should be skipped based on tolerance mode and custom presets"""
    global force_next_preset, current_preset
    
    if EE_IS_QT and not EE_SERVER_SUPPORTED:
        current_preset = get_current_preset_name()
    
    is_custom = current_preset not in [PRESET_SPEAKERS, PRESET_HEADPHONES]
    should_apply = True

    if TOLERANCE_MODE != "disabled" and is_custom:
        if TOLERANCE_MODE == "once":
            if not force_next_preset:
                should_apply = False
                force_next_preset = True
            else:
                force_next_preset = False
        elif TOLERANCE_MODE == "persistent":
            should_apply = False
    else:
        force_next_preset = False
        
    return is_custom, should_apply

def get_default_sink():
    """Get the current default audio sink name"""
    try:
        result = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=1)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("Timeout while getting default sink")
    except Exception as e:
        logger.warning(f"Failed to get default sink: {e}")
    return None

def find_g6_nodes():
    """Find all hidraw device nodes associated with the G6"""
    nodes = []
    hidraw_dir = '/sys/class/hidraw'
    
    if not os.path.exists(hidraw_dir):
        logger.error(f"Directory '{hidraw_dir}' is missing. Is the 'hidraw' kernel module loaded?")
        return nodes

    try:
        for dev in os.listdir(hidraw_dir):
            uevent_path = f'{hidraw_dir}/{dev}/device/uevent'
            try:
                with open(uevent_path, 'r') as f:
                    content = f.read().upper()
                    if '041E' in content and '3256' in content:
                        nodes.append(f'/dev/{dev}')
            except PermissionError:
                logger.error(f"Permission denied reading '{uevent_path}'. Check udev rules.")
            except FileNotFoundError:
                # Device might have been disconnected rapidly during enumeration
                continue
            except OSError as e:
                logger.error(f"OS/IO error while reading '{uevent_path}': {e}")
    except PermissionError:
        logger.error(f"Permission denied accessing directory '{hidraw_dir}'.")
    except Exception as e:
        logger.error(f"Unexpected error during G6 node enumeration: {e}")
        
    return nodes

def resolve_g6_audio_devices():
    """Resolve ALSA card index and G6 card name"""
    global g6_alsa_dev, g6_pa_card
    g6_alsa_dev = None
    g6_pa_card = None

    if USE_HW_PROFILES or WARN_ON_DSP_BOOT:
        try:
            with open('/proc/asound/cards', 'r') as f:
                for line in f:
                    if 'Sound BlasterX G6' in line:
                        match = re.search(r'^\s*(\d+)\s*\[', line)
                        if match:
                            g6_alsa_dev = match.group(1)
                            break
        except Exception as e:
            logger.error(f"Failed to resolve G6 ALSA card index: {e}. Hardware profile switching and DSP state checks are unavailable.")

    if USE_HW_PROFILES:
        try:
            result = subprocess.run(["pactl", "list", "cards", "short"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if 'Sound_BlasterX_G6' in line:
                    parts = line.split()
                    if len(parts) > 1:
                        g6_pa_card = parts[1]
                        break
        except Exception as e:
            logger.error(f"Failed to resolve G6 card name: {e}. Switching of hardware profiles is unavailable.")

def get_g6_initial_state(nodes, attempt=1):
    """Query the G6 via HID to determine the current output mode at startup"""
    query = bytes([0x5A, 0x2C, 0x01, 0x01]) + b'\x00' * 60

    for node in nodes:
        fd = None
        try:
            # Open node with non-blocking flag to prevent hanging
            fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
            os.write(fd, query)

            # Wait for the device to become readable with a 1.0 second timeout
            ready = select.select([fd], [], [], 1.0)
            if ready[0]:
                response = os.read(fd, 64)
                if len(response) >= 5 and response[0:4] == b'\x5A\x2C\x05\x01':
                    mode = response[4]
                    os.close(fd)
                    if mode == 0x04:
                        return "HEADPHONES"
                    elif mode == 0x02:
                        return "SPEAKERS"
            else:
                logger.warning(f"Timeout waiting for HID response from {node}")
        except Exception as e:
            # Catch permission or IO errors silently, proceed to next node or reset logic
            pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    # If we failed to get a state on the first attempt, try to reset the USB device
    if attempt == 1:
        logger.warning("Failed to determine initial state. Attempting to reset the USB device...")
        if shutil.which("usbreset"):
            try:
                subprocess.run(["usbreset", "041e:3256"], capture_output=True, timeout=5)
                # Wait for the kernel and udev to fully rebuild the device tree
                subprocess.run(["udevadm", "settle", "--timeout=5"])
            
                # Update the global active_hid_fds with new nodes after reset
                refresh_hid_nodes()
                
                resolve_g6_audio_devices()
                if active_hid_fds:
                    logger.info("USB reset executed. Retrying state query...")
                    return get_g6_initial_state(list(active_hid_fds.keys()), attempt=2)
                else:
                    logger.error("No Sound BlasterX G6 nodes found after reset.")
            except Exception as e:
                logger.error(f"Error executing usbreset: {e}")
        else:
            logger.warning("'usbreset' utility not found. Install it to enable automatic hardware recovery or replug the Sound BlasterX G6 manually.")
    return "UNKNOWN"

def initialize_g6(is_startup=False):
    """Initialization logic when Sound BlasterX G6 becomes the active output"""
    global g6_active, current_mode, first_run, last_applied_vol, last_speaker_volume, wireplumber_restart_pending
    g6_active = True
    last_applied_vol = -1
    logger.info("Sound BlasterX G6 output is active. Initializing...")

    # Suppress initial activation notification in daemon mode on boot to avoid notification spam
    if not (args.daemon and is_startup):
        send_notification("Sound BlasterX G6 is now active.")

    resolve_g6_audio_devices()
  
    # Check for the current DSP state
    if USE_HW_PROFILES or WARN_ON_DSP_BOOT:
        if g6_alsa_dev:
            try:
                with open(f'/proc/asound/card{g6_alsa_dev}/stream0', 'r') as f:
                    content = f.read()
                    if '384000' not in content:
                        if pipewire_active:
                            wireplumber_restart_pending = True
                        logger.warning("Sound BlasterX G6 booted in hardware DSP mode. High sample rates are locked.")
                        send_notification(f"Card booted in hardware DSP mode.\nPress and hold the 'Scout / Direct Mode' button for 2s to turn on 'Direct Mode'!")
        
                    elif wireplumber_restart_pending and pipewire_active:
                        logger.info("Sound BlasterX G6 now in hardware Direct Mode. Restarting WirePlumber to apply changes...")
                        try:
                            subprocess.run(["systemctl", "--user", "restart", "wireplumber"], capture_output=True, text=True, check=True)
                            subprocess.run(["gdbus", "wait", "--session", "--timeout", "5", "org.freedesktop.WirePlumber"], check=True)
                        except subprocess.CalledProcessError as e:
                            logger.error(f"Failed to restart WirePlumber: {e.stderr.strip()}")
                        except Exception as e:
                            logger.error(f"Failed to execute systemctl: {e}")
                    
                        wireplumber_restart_pending = False 
            except Exception as e:
                logger.error(f"Failed to check for DSP state: {e}")
    
    current_mode = get_g6_initial_state(list(active_hid_fds.keys()))

    if current_mode == "HEADPHONES":
        logger.info(">>> Initial state: HEADPHONES")
        if first_run:
            last_speaker_volume = get_current_volume_int()
        apply_hardware_profile("HEADPHONES")
        first_run = False
        set_preset(PRESET_HEADPHONES)
        set_volume(last_headphones_volume)
        apply_dynamic_processing(last_headphones_volume)
        sd_notify(f"STATUS=Active mode: HEADPHONES (Preset: {PRESET_HEADPHONES})\n")

    elif current_mode == "SPEAKERS":
        logger.info(">>> Initial state: SPEAKERS")
        apply_hardware_profile("SPEAKERS")
        set_preset(PRESET_SPEAKERS)
        if not first_run:
            set_volume(last_speaker_volume)
        sd_notify(f"STATUS=Active mode: SPEAKERS (Preset: {PRESET_SPEAKERS})\n")

    else:
        logger.warning("Failed to determine initial G6 state. Waiting for manual switch...")
        sd_notify("STATUS=Unknown G6 state, waiting for manual switch\n")

def refresh_hid_nodes():
    """Update the list of monitored HID nodes upon USB plug/unplug events"""
    global active_hid_fds
    current_paths = find_g6_nodes()

    # Remove disconnected HID nodes
    for path in list(active_hid_fds.keys()):
        if path not in current_paths:
            fd = active_hid_fds.pop(path)
            try:
                sel.unregister(fd)
                os.close(fd)
            except Exception:
                pass

    # Register newly detected HID nodes
    for path in current_paths:
        if path not in active_hid_fds:
            retries = 3
            while retries > 0:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    sel.register(fd, selectors.EVENT_READ, data="HID")
                    active_hid_fds[path] = fd
                    break
                except PermissionError:
                    time.sleep(0.5)
                    retries -= 1
                except Exception as e:
                    logger.error(f"Failed to open {path}: {e}")
                    break
            if retries == 0:
                logger.critical(f"Node {path} inaccessible after multiple attempts. Please configure udev rules. Aborting.")
                sys.exit(1)

def reset_to_default_preset():
    """Reset presets and state when switching away from the G6"""
    global g6_active, last_applied_vol, force_next_preset, current_mode

    if not g6_active:
        return

    time.sleep(0.5)
    if g6_sink_name in str(get_default_sink()):
        return

    g6_active = False
    last_applied_vol = -1
    force_next_preset = False
    real_preset = get_current_preset_name()

    if current_mode == "HEADPHONES":
        save_state()
        if real_preset == PRESET_HEADPHONES:
            set_preset(PRESET_DEFAULT)
    elif current_mode == "SPEAKERS":
        if real_preset == PRESET_SPEAKERS:
            set_preset(PRESET_DEFAULT)

    current_mode = "UNKNOWN"
    logger.info("Sound BlasterX G6 deactivated. Presets reset, monitoring suspended...")
    send_notification("Sound BlasterX G6 is no longer active.")
    sd_notify("STATUS=Sound BlasterX G6 inactive\n")

def reload_configuration():
    """Reload configuration from disk without restarting the service."""
    global config, config_errors
    global PRESET_HEADPHONES, PRESET_SPEAKERS, PRESET_DEFAULT
    global TOLERANCE_MODE, USE_SAFE_VOL, SAFE_VOL, SAVE_HP_VOL
    global USE_LIMITER, USE_HW_PROFILES, WARN_ON_DSP_BOOT, VOL_OFFSETS
    global last_headphones_volume, last_applied_vol

    logger.info("SIGHUP received. Reloading configuration...")

    if not CONFIG_FILE.exists():
        logger.error(f"Configuration file '{CONFIG_FILE}' not found. Reload aborted.")
        sd_notify(f"STATUS=Active mode: {current_mode} (Config reload failed: file not found)\n")
        if args.daemon:
            send_notification(f"Reload Error:\nConfiguration file not found! Please run the script with parameter '--setup'.", "dialog-error")
        return False

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse '{CONFIG_FILE}' (syntax error): {e}. Reload aborted.")
        sd_notify(f"STATUS=Active mode: {current_mode} (Config reload failed: JSON syntax error)\n")
        if args.daemon:
            send_notification(f"Reload Error:\nConfiguration syntax error! Check service logs (journalctl --user -xeu g6_switcher.service) for details.", "dialog-error")
        return False
    except Exception as e:
        logger.error(f"Failed to read '{CONFIG_FILE}': {e}. Reload aborted.")
        sd_notify(f"STATUS=Active mode: {current_mode} (Config reload failed: read error)\n")
        if args.daemon:
            send_notification(f"Reload Error:\nConfiguration read error! Check service logs (journalctl --user -xeu g6_switcher.service) for details.", "dialog-error")
        return False

    validated_config, config_errors = validate_and_fix_config(raw_data, DEFAULT_CONFIG)
    if config_errors:
        error_list = "\n".join([f"  - {err}" for err in config_errors])
        logger.warning(
            f"Configuration validation errors during reload. Invalid values were ignored and replaced with defaults for this session:\n"
            f"{error_list}\n"
            f"Please correct the configuration file ({CONFIG_FILE}) manually or run the script with parameter '--setup'."
        )  
        if args.daemon:
            send_notification(f"Reload Error:\nConfiguration error detected! Check service logs (journalctl --user -xeu g6_switcher.service) for details.", "dialog-warning")

    # Validate new presets existence
    presets = get_available_presets()
    if presets is not None:
        for p_key, p_global in [("headphones", "PRESET_HEADPHONES"),
                                ("speakers", "PRESET_SPEAKERS"),
                                ("default", "PRESET_DEFAULT")]:
            target_preset = validated_config["presets"][p_key]
            if target_preset not in presets:
                prev_val = globals()[p_global]
                logger.warning(
                    f"Preset '{target_preset}' (presets.{p_key}) not found in {APP_NAME}. "
                    f"Keeping previous value '{prev_val}'."
                )
                validated_config["presets"][p_key] = prev_val
    
    config = validated_config

    new_log_level = getattr(logging, config.get("log_level", "INFO").upper(), logging.INFO)
    logger.setLevel(new_log_level)

    old_hp_preset = PRESET_HEADPHONES
    old_spk_preset = PRESET_SPEAKERS
    old_use_limiter = USE_LIMITER

    PRESET_HEADPHONES = config["presets"]["headphones"]
    PRESET_SPEAKERS = config["presets"]["speakers"]
    PRESET_DEFAULT = config["presets"]["default"]

    TOLERANCE_MODE = config["features"]["preset_tolerance_mode"]
    USE_SAFE_VOL = config["features"]["use_safe_volume"]
    SAFE_VOL = config["features"]["safe_volume_percent"]
    SAVE_HP_VOL = config["features"]["save_headphones_volume"]
    WARN_ON_DSP_BOOT = config["features"]["warn_on_dsp_boot"]

    # Validate hardware profiles capability
    req_hw = config["features"]["use_hardware_profiles"]
    if req_hw and (not pipewire_active or not wireplumber_available or not shutil.which("amixer")):
        logger.warning("Hardware profile switching requires PipeWire, WirePlumber and amixer to handle dynamic sample rates. 'use_hardware_profiles' remains disabled.")
        USE_HW_PROFILES = False
    else:
        USE_HW_PROFILES = req_hw

    # Re-apply hardware profiles
    if g6_active:
        if USE_HW_PROFILES and current_mode in ("HEADPHONES", "SPEAKERS"):
            apply_hardware_profile(current_mode)
    
    # Validate limiter capability
    req_limiter = config["features"]["use_dynamic_limiter"]
    if req_limiter and EE_IS_QT and (not EE_SERVER_SUPPORTED or not EE_SERVER_SOCKET_PATH or inotify_init1 is None):
        logger.warning("Dynamic limiter is unavailable under current EasyEffects installation. 'use_dynamic_limiter' remains disabled.")
        USE_LIMITER = False
    else:
        USE_LIMITER = req_limiter

    VOL_OFFSETS = {int(k): float(v) for k, v in config["dynamic_limiter_offsets"].items()}

    if not SAVE_HP_VOL and USE_SAFE_VOL:
        last_headphones_volume = SAFE_VOL

    # Re-apply preset if it was renamed while active
    if g6_active:
        if current_mode == "HEADPHONES" and current_preset == old_hp_preset and PRESET_HEADPHONES != old_hp_preset:
            set_preset(PRESET_HEADPHONES)
        elif current_mode == "SPEAKERS" and current_preset == old_spk_preset and PRESET_SPEAKERS != old_spk_preset:
            set_preset(PRESET_SPEAKERS)

    # Re-evaluate dynamic limiter if currently in headphones mode
    if current_mode == "HEADPHONES" and g6_active:
        last_applied_vol = -1
        if USE_LIMITER and current_preset == PRESET_HEADPHONES:
            apply_dynamic_processing(get_current_volume_int())
        elif old_use_limiter and not USE_LIMITER:
            # Reset limiter threshold back to 0.0 dB if feature was disabled
            if EE_IS_QT and EE_SERVER_SUPPORTED and EE_SERVER_SOCKET_PATH:
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(0.5)
                        s.connect(EE_SERVER_SOCKET_PATH)
                        s.sendall(b"set_property:output:limiter:0:threshold:0.0\n")
                except Exception:
                    pass
            elif not EE_IS_QT:
                cmd = ["flatpak", "run", "--command=gsettings", SCHEMA_BASE, "set", LIMITER_PATH, LIMITER_KEY, "0.0"] if flatpak_used else ["gsettings", "set", LIMITER_PATH, LIMITER_KEY, "0.0"]
                subprocess.run(cmd, capture_output=True, timeout=1)

    logger.info("Configuration reloaded.")
    sd_notify(f"STATUS=Active mode: {current_mode} (Preset: {current_preset})\n")
    if args.daemon:
        send_notification("Configuration reloaded.", "preferences-system")
    return True

def is_system_shutting_down():
    """Check if the OS or user session is shutting down."""
    try:
        sys_state = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=1).stdout.strip()
        if sys_state == "stopping":
            return True

        user_state = subprocess.run(["systemctl", "--user", "show", "-p", "ManagerState"], capture_output=True, text=True, timeout=1).stdout.strip()
        if user_state == "ManagerState=stopping":
            return True
    except Exception:
        pass
    return False

def cleanup():
    global cleanup_done
    if cleanup_done:
        return
    cleanup_done = True
    sd_notify("STOPPING=1\nSTATUS=Stopping service...\n")

    if received_signal:
        sig_name = signal.Signals(received_signal).name
        logger.info(f"Process terminated by {sig_name}. Cleaning up and resetting to default...")
    else:
        logger.info("Process terminating. Cleaning up and resetting to default...")
    
    shutting_down = is_system_shutting_down()
    if shutting_down:
        logger.info("System or session shutdown detected. Skipping audio state reset.")
    
    try:
        global first_run, last_headphones_volume
        if g6_active:
            if current_mode == "HEADPHONES":
                last_headphones_volume = get_current_volume_int()
                save_state()
            if not shutting_down:
                set_preset(PRESET_DEFAULT)
        if not first_run and not shutting_down:
            set_volume(last_speaker_volume)
    except Exception as e:
        logger.error(f"Error while resetting audio state: {e}")

    for name in ("pulse_proc", "gsettings_proc"):
        proc = globals().get(name)
        if proc is None:
            continue

        try:
            proc.terminate()
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            logger.warning(f"{name} did not terminate in time; killing it.")
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception as e:
                logger.error(f"Failed to kill {name}: {e}")
        except Exception as e:
            logger.error(f"Error while stopping {name}: {e}")

    try:
        global inotify_fd
        if 'inotify_fd' in globals() and inotify_fd >= 0:
            sel.unregister(inotify_fd)
            os.close(inotify_fd)
            inotify_fd = -1
    except Exception as e:
        logger.error(f"Failed to close inotify fd: {e}")
    
    try:
        signal.set_wakeup_fd(-1)
    except Exception as e:
        logger.error(f"Failed to disable signal wakeup fd: {e}")

    try:
        sel.unregister(wakeup_r)
    except Exception as e:
        logger.error(f"Failed to unregister wakeup fd: {e}")

    for fd in (wakeup_r, wakeup_w):
        try:
            os.close(fd)
        except OSError as e:
            logger.error(f"Failed to close wakeup pipe fd {fd}: {e}")

# === STARTUP BANNER ===
if not args.daemon:
    print("="*59)
    print(f"           Sound BlasterX G6 Switcher v{SCRIPT_VERSION}")
    print("="*59)
    print(f" Powered by {ENGINE_INFO}\n")
    print(f" Use '-h' for help.\n")
    print(" GitHub: https://github.com/SmArT-32-projects/G6-switcher")
    print(" Copyright (c) 2026 SmArT-32-projects | MIT License")
    print("="*59)
    print(f" Config path: {CONFIG_FILE}")
    print(f"   Headphones preset:   '{PRESET_HEADPHONES}'")
    print(f"   Speakers preset:     '{PRESET_SPEAKERS}'")
    print(f"   Default preset:      '{PRESET_DEFAULT}'")
    print(f"   Preset tolerance:     {TOLERANCE_MODE}")
    print(f"   Safe volume enabled:  {USE_SAFE_VOL} ({SAFE_VOL}%)")
    print(f"   Dynamic limiter:      {USE_LIMITER}")
    print(f"   Save HP volume:       {SAVE_HP_VOL}")
    print(f"   Hardware profiles:    {USE_HW_PROFILES}")
    print(f"   Warning on DSP boot:  {WARN_ON_DSP_BOOT}")
    print("="*59)
else:
    logger.info(f"Sound BlasterX G6 Switcher v{SCRIPT_VERSION} service successfully started in background (daemon mode).")

refresh_hid_nodes()
sink_line = get_default_sink()
last_default_sink = sink_line

if not active_hid_fds:
    logger.info("Sound BlasterX G6 not found. Waiting for device to be connected...")
elif g6_sink_name in str(sink_line):
    initialize_g6(is_startup=True)
else:
    logger.info("Sound BlasterX G6 output is inactive. Waiting for sink switch...")

# Start monitor processes (pactl for volume events, gsettings or inotify for preset changes)
pulse_proc = subprocess.Popen(
    ["pactl", "subscribe"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    env={**os.environ, "LC_MESSAGES": "C"}
)

gsettings_proc = None
inotify_fd = -1

if not EE_IS_QT:
    if flatpak_used:
        gsettings_cmd = ["flatpak", "run", "--command=gsettings", SCHEMA_BASE, "monitor", SCHEMA_BASE, PRESET_KEY]
    else:
        gsettings_cmd = ["gsettings", "monitor", SCHEMA_BASE, PRESET_KEY]

    gsettings_proc = subprocess.Popen(gsettings_cmd, stdout=subprocess.PIPE, text=True)
    os.set_blocking(gsettings_proc.stdout.fileno(), False)
    sel.register(gsettings_proc.stdout, selectors.EVENT_READ, data="GSETTINGS")

elif EE_SERVER_SUPPORTED and inotify_init1:
    inotify_fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
    if inotify_fd >= 0:
        
        wd = inotify_add_watch(inotify_fd, str(EE_QT_DATA_DIR).encode('utf-8'), IN_OPEN)
        if wd >= 0:
            sel.register(inotify_fd, selectors.EVENT_READ, data="INOTIFY")
        else:
            logger.critical(f"Failed to add inotify watch: {os.strerror(ctypes.get_errno())}")
            sys.exit(1)
    else:
        logger.critical(f"Failed to initialize inotify: {os.strerror(ctypes.get_errno())}")
        sys.exit(1)


os.set_blocking(pulse_proc.stdout.fileno(), False)
sel.register(pulse_proc.stdout, selectors.EVENT_READ, data="PACTL")

current_preset = get_current_preset_name()
logger.info("Monitoring started successfully.")

if g6_active:
    sd_notify(f"READY=1\nSTATUS=Active mode: {current_mode} (Preset: {current_preset})\n")
else:
    sd_notify("READY=1\nSTATUS=Sound BlasterX G6 inactive\n")

# Register signal handlers
shutdown_requested = False
reload_requested = False
received_signal = None
cleanup_done = False

def signal_handler(signum, frame):
    global shutdown_requested, reload_requested, received_signal
    if signum == signal.SIGHUP:
        reload_requested = True
    else:
        shutdown_requested = True
        received_signal = signum

wakeup_r, wakeup_w = os.pipe() # Prepare wakeup mechanism first
os.set_blocking(wakeup_r, False)
os.set_blocking(wakeup_w, False)
sel.register(wakeup_r, selectors.EVENT_READ, data="SIGNAL")
signal.set_wakeup_fd(wakeup_w)

signal.signal(signal.SIGINT, signal_handler)   # Handles Ctrl+C
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)    # Reload configuration

# === MAIN EVENT LOOP ===
try:
    while not shutdown_requested:
        events = sel.select()
        for key, _ in events:
            
            # 1. SIGNAL EVENT PROCESSING
            if key.data == "SIGNAL":
                try:
                    os.read(key.fd, 1024)
                except OSError:
                    pass
                if shutdown_requested:
                    break # Stop processing this event batch immediately
                if reload_requested:
                    reload_requested = False
                    reload_configuration()
                    continue
                
            # Also stop the current event batch immediately if the shutdown
            # flag is already set (signal may have arrived mid-batch).
            if shutdown_requested:
                break

            # 2. HID EVENT PROCESSING (Headphones/Speakers Switch)
            if key.data == "HID":
                # Read immediately to clear the buffer and prevent infinite loop spinning
                try:
                    data = os.read(key.fd, 64)
                except OSError:
                    # Device was physically disconnected
                    refresh_hid_nodes()
                    continue
                
                if not g6_active:
                    continue

                clean_data = data.rstrip(b'\x00')
                line = clean_data.hex().upper()

                if not line: continue

                if "5A2C0501" in line:
                    # Format the hex string for easier debugging (e.g., "5A 2C 05 01 ...")
                    formatted_line = " ".join([line[i:i+2] for i in range(0, len(line), 2)])
                    logger.debug(f"RAW MATCH: {formatted_line}")
                    
                    idx = line.find("5A2C0501") + 8
                    state_byte = line[idx:idx+2]

                    if state_byte == "04":
                        if current_mode == "HEADPHONES":
                            continue
                        logger.info(">>> Mode Switched: HEADPHONES")
                        current_mode = "HEADPHONES"
                        last_speaker_volume = get_current_volume_int()
                        first_run = False
                        logger.debug(f"Saved speaker volume: {last_speaker_volume}%")
                        apply_hardware_profile("HEADPHONES")

                        is_custom, should_apply = check_preset_tolerance()
                        
                        if not should_apply:
                            logger.info(f"Custom preset '{current_preset}' detected. Skipping automatic activation of '{PRESET_HEADPHONES}'.")
                            send_notification(f"Custom preset active. Skipping auto-switching to '{PRESET_HEADPHONES}'.", "audio-headphones")
                            
                            last_headphones_volume = SAFE_VOL if USE_SAFE_VOL else get_current_volume_int()
                            set_volume(last_headphones_volume)
                            sd_notify(f"STATUS=Active mode: HEADPHONES (Preset: {current_preset})\n")
                        else:
                            set_preset(PRESET_HEADPHONES)
                            if is_custom:
                                last_headphones_volume = SAFE_VOL if USE_SAFE_VOL else get_current_volume_int()
                        
                            set_volume(last_headphones_volume)
                            last_applied_vol = -1
                            apply_dynamic_processing(last_headphones_volume)
                            sd_notify(f"STATUS=Active mode: HEADPHONES (Preset: {current_preset})\n")

                    elif state_byte == "02":
                        if current_mode == "SPEAKERS":
                            continue
                        logger.info(">>> Mode Switched: SPEAKERS")
                        if SAVE_HP_VOL:
                            last_headphones_volume = get_current_volume_int()
                            save_state()
                        current_mode = "SPEAKERS"
                        apply_hardware_profile("SPEAKERS")
                        
                        is_custom, should_apply = check_preset_tolerance()
                        
                        if not should_apply:
                            logger.info(f"Custom preset '{current_preset}' detected. Skipping automatic activation of '{PRESET_SPEAKERS}'.")
                            send_notification(f"Custom preset active. Skipping auto-switching to '{PRESET_SPEAKERS}'.", "audio-speakers")
                            if not first_run:
                                set_volume(last_speaker_volume)
                                sd_notify(f"STATUS=Active mode: SPEAKERS (Preset: {current_preset})\n")
                        else:
                            set_preset(PRESET_SPEAKERS)
                            if not first_run:
                                set_volume(last_speaker_volume)
                                sd_notify(f"STATUS=Active mode: SPEAKERS (Preset: {current_preset})\n")
                            else:
                                logger.debug("Skipping speakers volume setup on first run.")
                    else:
                        logger.error(f"Unknown HID state byte received: {state_byte}")

            # 3. PACTL EVENT PROCESSING (volume events and default sink changes)
            elif key.data == "PACTL":
                try:
                    line = pulse_proc.stdout.readline()
                except BlockingIOError:
                    continue # Incomplete line, wait for the next selector cycle

                # Handle pactl crash/exit
                if pulse_proc.poll() is not None or not line:
                    if is_system_shutting_down():
                        # System is going down, exit silently
                        sys.exit(0)
                    
                    if pulse_proc.poll() is None:
                        pulse_proc.kill()  # Hung process with empty pipe, force kill
                        try:
                            pulse_proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass

                    exit_code = pulse_proc.poll()
                    try:
                        leftovers = pulse_proc.stdout.read()
                        error_msg = leftovers.strip() if leftovers else f"Unknown pactl error (code {exit_code})"
                    except Exception:
                        error_msg = f"Failed to read error reason (code {exit_code})"

                    logger.error(f"pactl process crashed unexpectedly. Reason: {error_msg}. Attempting recovery...")
                    send_notification("Media server error, preset auto-switching paused.")

                    # Cleanup old process
                    sel.unregister(pulse_proc.stdout)
                    pulse_proc.stdout.close()
                    try:
                        pulse_proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass

                    retries = 5
                    while retries > 0:
                        time.sleep(1)
                        if subprocess.run(["pactl", "info"], capture_output=True).returncode == 0:
                            logger.info("Media server is back online. Re-initializing...")
                            break
                        retries -= 1
                    if retries == 0:
                        logger.critical("Failed to reconnect to media server. Aborting.")
                        sys.exit(1)

                    # Restart audio engine
                    if flatpak_used:
                        subprocess.run(["flatpak", "kill", SCHEMA_BASE], check=False, stderr=subprocess.DEVNULL)
                    else:
                        subprocess.run(["pkill"] + APP_NAME_CMD, stderr=subprocess.DEVNULL)
                    ENGINE_ACTIVE = False
                    
                    time.sleep(0.5)
                    if args.daemon:
                        subprocess.run(
                            ["systemd-run", "--user", "--collect", f"--unit=g6-switcher-{APP_NAME.lower()}-service",
                            f"--description={APP_NAME} Service for Sound BlasterX G6 Switcher",
                            *APP_NAME_CMD, "--gapplication-service"],
                            check=False, capture_output=True, timeout=5
                        )
                    else:
                        subprocess.Popen(
                            APP_NAME_CMD + ["--gapplication-service"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, start_new_session=True
                        )
                    
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if is_engine_running():
                            ENGINE_ACTIVE = True
                            logger.info(f"{ENGINE_INFO} restarted successfully.")
                            break
                        time.sleep(1)
        
                    if not ENGINE_ACTIVE:
                        logger.critical(f"Error restarting {APP_NAME} (startup timeout). Aborting.")
                        sys.exit(1)

                    # Restart pactl subscription
                    pulse_proc = subprocess.Popen(
                        ["pactl", "subscribe"],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                        env={**os.environ, "LC_MESSAGES": "C"}
                    )
                    os.set_blocking(pulse_proc.stdout.fileno(), False)
                    sel.register(pulse_proc.stdout, selectors.EVENT_READ, data="PACTL")
                    logger.info("Monitoring restarted.")
                    send_notification(f"Preset auto-switching restored for {APP_NAME}.")

                    # Reinitialize G6 state
                    refresh_hid_nodes()
                    last_default_sink = get_default_sink()
                    if g6_sink_name in str(last_default_sink) and active_hid_fds:
                        initialize_g6()
                    else:
                        reset_to_default_preset()
                    continue

                # Process valid pactl events
                if "Event 'new' on card #" in line or "Event 'remove' on card #" in line:
                    refresh_hid_nodes()
                    sink_line = get_default_sink()
                    if g6_sink_name in str(sink_line) and active_hid_fds:
                        if not g6_active:
                            initialize_g6()
                    else:
                        if g6_active:
                            reset_to_default_preset()

                if "Event 'change' on server #" in line:
                    current_sink = get_default_sink()
                    if current_sink is not None and current_sink != last_default_sink:
                        old_sink = last_default_sink
                        last_default_sink = current_sink
                        if g6_sink_name in current_sink:
                            if not active_hid_fds:
                                refresh_hid_nodes()
                            if not g6_active and active_hid_fds:
                                initialize_g6()
                        else:
                            if g6_active:
                                if current_mode == "HEADPHONES" and old_sink and g6_sink_name in str(old_sink):
                                    last_headphones_volume = get_current_volume_int(old_sink)
                                    save_state()
                                reset_to_default_preset()

                elif "Event 'change' on sink #" in line:
                    if g6_active and (USE_LIMITER or SAVE_HP_VOL):
                        v = get_current_volume_int()
                        if v != last_seen_vol:
                            last_seen_vol = v
                            
                            # Update global variables on the fly for both modes
                            if current_mode == "HEADPHONES":
                                last_headphones_volume = v
                                if USE_LIMITER:
                                    if current_preset == PRESET_HEADPHONES:
                                        if v != vol_applied_by_script:
                                            apply_dynamic_processing(v)
                                        vol_applied_by_script = -1
                                    else:
                                        last_applied_vol = -1
                                else:
                                    vol_applied_by_script = -1
                            elif current_mode == "SPEAKERS":
                                last_speaker_volume = v
                                if v != vol_applied_by_script:
                                    vol_applied_by_script = -1

            # 4. GSETTINGS EVENT PROCESSING (Preset tracking)
            elif key.data == "GSETTINGS":
                try:
                    line = gsettings_proc.stdout.readline()
                except BlockingIOError:
                    continue

                if gsettings_proc.poll() is not None or not line:
                    time.sleep(0.2)
                    if is_system_shutting_down():
                        # System is going down, exit silently
                        sys.exit(0)
                    
                    if gsettings_proc.poll() is None:
                        gsettings_proc.kill()
                        try:
                            gsettings_proc.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            pass

                    logger.warning("gsettings process (preset monitor) crashed. Attempting recovery...")

                    sel.unregister(gsettings_proc.stdout)
                    gsettings_proc.stdout.close()
                    try:
                        gsettings_proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass

                    retries = 5
                    while retries > 0:
                        time.sleep(1)
                        # Probe gsettings key access to verify D-Bus and schema availability
                        if flatpak_used:
                            check_dbus = subprocess.run(
                                ["flatpak", "run", "--command=gsettings", SCHEMA_BASE, "get", SCHEMA_BASE, PRESET_KEY],
                                capture_output=True, timeout=5
                            )
                        else:
                            check_dbus = subprocess.run(
                                ["gsettings", "get", SCHEMA_BASE, PRESET_KEY],
                                capture_output=True, timeout=5
                            )
                        if check_dbus.returncode == 0:
                            logger.info("D-Bus GSettings access restored. Re-initializing...")
                            break
                        retries -= 1

                    if retries == 0:
                        logger.critical("Failed to reconnect to GSettings (D-Bus unavailable). Aborting.")
                        sys.exit(1)

                    gsettings_proc = subprocess.Popen(gsettings_cmd, stdout=subprocess.PIPE, text=True)
                    os.set_blocking(gsettings_proc.stdout.fileno(), False)
                    sel.register(gsettings_proc.stdout, selectors.EVENT_READ, data="GSETTINGS")

                    # Force-refresh preset state in case it changed while monitoring was down
                    current_preset = get_current_preset_name()
                    logger.info("GSettings monitoring restarted.")
                    continue

                if line.startswith(f"{PRESET_KEY}:"):
                    current_preset = line.split(":", 1)[1].strip().strip("'")
                    if current_preset != preset_applied_by_script:
                        logger.debug(f"Output preset switched manually to '{current_preset}'")
                        preset_applied_by_script = ""
                    
                    # User manually switched to a custom preset and then returned to the default headphones preset
                    if current_mode == "HEADPHONES" and current_preset == PRESET_HEADPHONES and preset_applied_by_script == "":
                        last_applied_vol = -1
                        apply_dynamic_processing(last_headphones_volume)

            # 5. INOTIFY EVENT PROCESSING (Qt Preset tracking)
            elif key.data == "INOTIFY":
                try:
                    data = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                except OSError as e:
                    logger.critical(f"Error reading inotify events: {e}")
                    sys.exit(1)

                offset = 0
                
                # Parse struct inotify_event structures
                # int wd, uint32 mask, uint32 cookie, uint32 len
                event_size = struct.calcsize("iIII")
                preset_opened = False
                
                while offset + event_size <= len(data):
                    wd, mask, cookie, name_len = struct.unpack_from("iIII", data, offset)
                    offset += event_size
                    
                    if mask & IN_Q_OVERFLOW:
                        logger.critical("inotify event queue overflowed. Preset monitoring is down.")
                        sys.exit(1)
                    if mask & IN_IGNORED:
                        logger.critical("EasyEffects preset directory inotify watch was removed or unmounted.")
                        sys.exit(1)
                    
                    if name_len:
                        if offset + name_len > len(data):
                            logger.critical("Malformed inotify event received.")
                            sys.exit(1)
                        
                        name_bytes = data[offset:offset+name_len]
                        offset += name_len
                        
                        if mask & IN_OPEN:
                            # Extract name up to the first null byte
                            name = name_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='ignore')
                            if name.endswith(".json"):
                                preset_opened = True

                if preset_opened:
                    logger.debug("Preset file open event detected.")
                    detected_preset = get_current_preset_name()
                    
                    if detected_preset and detected_preset != current_preset:
                        current_preset = detected_preset
                        if current_preset != preset_applied_by_script:
                            logger.debug(f"Output preset switched manually to '{current_preset}'")
                            preset_applied_by_script = ""
                        
                        # User manually switched to a custom preset and then returned to the default headphones preset
                        if current_mode == "HEADPHONES" and current_preset == PRESET_HEADPHONES and preset_applied_by_script == "":
                            last_applied_vol = -1
                            apply_dynamic_processing(last_headphones_volume)
                        
except Exception as e:
    logger.critical(f"Unhandled exception in main loop: {e}")
    sys.exit(1)
finally:
    cleanup()
