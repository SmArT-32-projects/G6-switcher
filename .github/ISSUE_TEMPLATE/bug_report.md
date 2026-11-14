---
name: Bug report
about: Report a problem with Sound BlasterX G6 Switcher
title: "[BUG]"
labels: bug
assignees: ''

---

**Pre-flight Checklist**

- [ ] I have read the [Known limitations](https://github.com/SmArT-32-projects/g6-switcher#known-limitations) section in the README.
- [ ] I have reviewed the [Troubleshooting guide](https://github.com/SmArT-32-projects/g6-switcher/blob/main/docs/troubleshooting.md).
- [ ] My Sound BlasterX G6 is running the latest firmware.
- [ ] My sound card is recognized in `cat /proc/asound/cards`.

**Describe the bug**

A clear and concise description of what the bug is.

**To Reproduce**

1. ...
2. ...
3. ...

**Expected behavior**

A clear and concise description of what you expected to happen.

**Relevant logs**

Set `"log_level": "DEBUG"` in `~/.config/g6_switcher/config.json`, reload the service (`systemctl --user reload g6_switcher.service`), reproduce the issue, and paste output from:

```bash
journalctl --user -u g6_switcher.service -n 100 --no-pager
```
If you run G6 Switcher manually, include the output from the terminal instead.

**Screenshots**

If applicable, add screenshots to help explain your problem.

**System Environment**

 - **G6 Switcher version**: (e.g. `1.0` or commit hash)
 - **Linux distribution / version**: (e.g. Debian 13.7, Fedora 45)
 - **Linux kernel version**: (`uname -r`)
 - **Sound server**: (e.g. PipeWire 1.2.6 / WirePlumber 0.5.6 or PulseAudio 17.0)
 - **Python version**: (e.g. Python 3.15.2)
 - **Effects app & version**: (e.g. EasyEffects 8.3.0 Flatpak / native)

**Additional context**

Add any other context about the problem here.
