# logi-mouse-follow

Make a Logitech mouse follow your Logitech keyboard when you press an
Easy-Switch key, across **Windows and Linux**, including mice that Logi
Options+ won't link (such as the MX Master 2S).

Logitech's own "Enhanced Easy-Switch" only works with newer mice and only
between computers that run Logi Options+, which isn't available on Linux.
This script does the same job with a small Python program on each computer.

## How it works

Run `mouse_follow.py` on every computer you switch between. It watches the
keyboard, and when the keyboard leaves that computer (because you pressed an
Easy-Switch key), it tells the mouse to switch to the other computer's
channel.

It checks the keyboard's connection without depending on whether the
keyboard is awake:

| Keyboard connected via | How the script checks it |
| --- | --- |
| Unifying / Bolt receiver | Asks the receiver whether the keyboard's link is up |
| Bluetooth (Linux) | Checks whether the Bluetooth device exists |

The script uses Logitech's HID++ protocol (the same commands
[Solaar](https://github.com/pwr-Solaar/Solaar) uses). It never reads your
keystrokes, the keyboard's Easy-Switch keys keep working normally, and
nothing is changed permanently on the devices.

Tested with an MX Keys S (Bolt receiver on Windows, Bluetooth on Linux) and
an MX Master 2S (Unifying receiver on Windows, Bluetooth on Linux). Other
Easy-Switch devices should work too.

## Requirements

- Python 3.8+
- **Windows:** `pip install hidapi`, and **Logi Options+ must not be running**
  (it blocks access to the devices)
- **Linux:** no extra Python packages, but you need read/write access to
  `/dev/hidraw*`. The easiest way is to install Solaar, which adds the right
  udev rules: `sudo apt install solaar`, then
  `sudo udevadm control --reload-rules && sudo udevadm trigger`

## Setup

Do this on each computer, with the keyboard and mouse switched to it.

1. **Check the devices are found.** Wake them (press a key, move the mouse)
   and run:

   ```
   python mouse_follow.py --list
   ```

   You should see both devices, the channel each is on, and a presence check
   of `receiver` or `bluetooth`.

2. **Work out `--mouse-target`.** This is the *mouse's* Easy-Switch channel
   for the **other** computer (it may differ from the keyboard's numbering).
   Test it:

   ```
   python mouse_follow.py --test-switch 2
   ```

3. **Try it out:**

   ```
   python mouse_follow.py --mouse-target 2 --debug
   ```

   Press the keyboard's Easy-Switch key for the other computer; the mouse
   should follow within a few seconds.

4. **Start it automatically at login.**

   - **Windows:** press Win+R, type `shell:startup`, create a new shortcut
     with this target (use the full path to `pythonw.exe` if Windows can't
     find it, see `where.exe pythonw`):

     ```
     pythonw "C:\path\to\mouse_follow.py" --mouse-target 2 --notify errors
     ```

     Also stop Logi Options+ starting with Windows (Task Manager → Startup
     apps).

   - **Linux (GNOME / Zorin / Ubuntu):** open *Startup Applications* and add:

     ```
     python3 /path/to/mouse_follow.py --mouse-target 1 --notify errors
     ```

## Options

| Option | Meaning |
| --- | --- |
| `--mouse-target N` | Mouse channel (1–3) of the other computer. Required to run. |
| `--keyboard NAME` | Part of the keyboard's name (default `MX Keys`) |
| `--mouse NAME` | Part of the mouse's name (default `MX Master`) |
| `--notify all\|errors\|off` | Desktop notifications for every switch, only failures, or none (default `all`) |
| `--retry-seconds N` | Keep trying to move a sleeping mouse for this long (default 20) |
| `--interval N` | Seconds between checks (default 1.5) |
| `--log-file PATH` | Also write a log (capped at about 1 MB) |
| `--debug` | Detailed output, including the raw HID++ traffic |
| `--list` | List devices, channels and how each is checked, then exit |
| `--test-switch N` | Send the mouse to channel N and exit |
| `--test-notify` | Show a test notification and exit |
| `--allow-ping` | Run even if the keyboard can only be checked by pinging (unreliable, see below) |

## Limitations and troubleshooting

- **The script must be running on the computer the keyboard is leaving.** If
  that computer is asleep, off, or not logged in, press the channel button
  under the mouse.
- **The keyboard must be awake when the script starts** so it can be found.
  The script keeps looking every few seconds until it is.
- **A sleeping mouse moves once you touch it.** The script keeps retrying
  for `--retry-seconds`.
- **Linux takes a few seconds** to notice a Bluetooth keyboard has gone, so
  switches from Linux are a little slower.
- **Only one other computer is supported per instance** (`--mouse-target` is
  a single channel).
- **"Can't check the keyboard's connection reliably"**: the receiver didn't
  answer the link-status query, so the only option left is pinging the
  keyboard, and a dozing keyboard doesn't answer pings. The script refuses to
  run in that mode by default. Run `--list --debug` to see why.
- **Nothing found on Windows:** quit Logi Options+, wake the devices, and
  try again.
- **Nothing found on Linux:** check permissions on `/dev/hidraw*` (see
  Requirements).

## Licence

MIT, see [LICENSE](LICENSE).
