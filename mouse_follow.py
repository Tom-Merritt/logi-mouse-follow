#!/usr/bin/env python3
"""
mouse_follow.py - make a Logitech mouse follow a Logitech keyboard's Easy-Switch.

Run this on BOTH computers. It watches the keyboard; when the keyboard leaves
this computer (because you pressed an Easy-Switch key), it tells the mouse to
switch to the channel given with --mouse-target (the mouse's channel for the
*other* computer).

How it knows the keyboard has left:
  * keyboard on a USB receiver (Unifying/Bolt): asks the receiver whether the
    keyboard's link is up. A sleeping keyboard still counts as connected.
  * keyboard on Bluetooth (Linux): checks whether the Bluetooth device exists.
  * anything else: falls back to pinging the keyboard, guarded by --idle-guard.

It talks to the devices using Logitech's HID++ protocol (the same commands
Solaar uses). Nothing is changed permanently on the devices.

Usage:
  python mouse_follow.py --list                      # show devices + channels
  python mouse_follow.py --test-switch 2             # send the mouse to channel 2 now
  python mouse_follow.py --mouse-target 2            # run (watch keyboard, move mouse)
  add --debug to any of these for detailed output

Requirements:
  Windows: pip install hidapi   (and quit Logi Options+, which blocks access)
  Linux:   no extra packages; needs read/write access to /dev/hidraw*
           (installing the 'solaar' package adds the udev rules for this).
"""

import argparse
import glob
import logging
import os
import re
import select
import subprocess
import sys
import time

IS_WINDOWS = sys.platform.startswith("win")

LOGI_VID = 0x046D
SW_ID = 0x0A           # arbitrary software id, lets us spot our own replies
REPORT_SHORT = 0x10
REPORT_LONG = 0x11
SHORT_LEN = 7
LONG_LEN = 20

FEAT_DEVICE_NAME = 0x0005
FEAT_CHANGE_HOST = 0x1814

# HID++ 1.0 receiver registers
SET_REGISTER = 0x80
GET_REGISTER = 0x81
REG_NOTIFICATIONS = 0x00
REG_CONNECTION = 0x02
NOTIF_DEVICE_CONNECTION = 0x41

# 0xFF = device connected directly (Bluetooth); 1-6 = slots on a receiver
DEVICE_INDICES = (0xFF, 1, 2, 3, 4, 5, 6)

log = logging.getLogger("mouse-follow")


class HidppError(Exception):
    pass


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class LinuxHidraw:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def write(self, data):
        os.write(self.fd, data)

    def read(self, timeout_ms):
        r, _, _ = select.select([self.fd], [], [], max(timeout_ms, 0) / 1000)
        if not r:
            return None
        return os.read(self.fd, 64)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class WindowsHid:
    def __init__(self, path):
        import hid  # from: pip install hidapi
        self.path = path
        self.dev = hid.device()
        self.dev.open_path(path)

    def write(self, data):
        if self.dev.write(bytes(data)) < 0:
            raise OSError("HID write failed")

    def read(self, timeout_ms):
        data = self.dev.read(64, max(int(timeout_ms), 1))
        return bytes(data) if data else None

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass


def open_transport(path):
    return WindowsHid(path) if IS_WINDOWS else LinuxHidraw(path)


def _collection_key(path):
    """Strip the parts of a Windows HID path that differ between collections of
    the same interface: '&ColNN' and the trailing instance number ('&0001')."""
    p = re.sub(rb"&col\d+", b"", path.lower())
    return re.sub(rb"&[0-9a-f]+#\{", b"#{", p)


def candidate_paths():
    """HID paths that might carry long HID++ messages for Logitech devices."""
    if IS_WINDOWS:
        import hid
        paths = []
        for d in hid.enumerate(LOGI_VID, 0):
            # long HID++ collection on receivers (0xFF00/0x0002) and
            # on Bluetooth devices (0xFF43/0x0202)
            if (d["usage_page"], d["usage"]) in ((0xFF00, 0x0002), (0xFF43, 0x0202)):
                if d["path"] not in paths:
                    paths.append(d["path"])
        return paths

    paths = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        ue = linux_uevent("/dev/" + os.path.basename(node))
        parts = ue.get("HID_ID", "").split(":")  # e.g. 0003:0000046D:0000C548
        if len(parts) == 3 and int(parts[1], 16) == LOGI_VID:
            paths.append("/dev/" + os.path.basename(node))
    return paths


def windows_short_path(long_path):
    """On Windows the short HID++ messages (used for receiver registers and
    connection notifications) live in a separate collection."""
    import hid
    key = _collection_key(long_path)
    shorts = []
    for d in hid.enumerate(LOGI_VID, 0):
        if (d["usage_page"], d["usage"]) == (0xFF00, 0x0001):
            shorts.append(d["path"])
            if _collection_key(d["path"]) == key:
                return d["path"]
    log.debug("No short channel matched %s; candidates were: %s", long_path, shorts)
    return None


def linux_uevent(dev_path):
    node = os.path.join("/sys/class/hidraw", os.path.basename(dev_path), "device", "uevent")
    try:
        with open(node) as f:
            return dict(l.strip().split("=", 1) for l in f if "=" in l)
    except OSError:
        return {}


def linux_bus(dev_path):
    """'0005' = Bluetooth, '0003' = USB."""
    return linux_uevent(dev_path).get("HID_ID", "").split(":")[0]


# --------------------------------------------------------------------------
# HID++ helpers
# --------------------------------------------------------------------------

def drain(t):
    while t.read(1):
        pass


def request(t, dev, feat_idx, func, params=b"", timeout_ms=500):
    """Send a long HID++ 2.0 request and return the reply payload (bytes after
    the 4-byte header), None on timeout, or raise HidppError on an error reply."""
    fn = (func << 4) | SW_ID
    msg = bytes([REPORT_LONG, dev, feat_idx, fn]) + bytes(params)
    msg = msg.ljust(LONG_LEN, b"\0")
    drain(t)
    t.write(msg)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            return None
        r = t.read(remaining)
        if not r or len(r) < 5 or r[0] not in (REPORT_SHORT, REPORT_LONG) or r[1] != dev:
            continue
        if r[2] == feat_idx and r[3] == fn:
            return r[4:]
        if r[2] in (0x8F, 0xFF) and r[3] == feat_idx and r[4] == fn:
            raise HidppError(r[5] if len(r) > 5 else 0)


def ping(t, dev, timeout_ms=500):
    try:
        r = request(t, dev, 0x00, 1, b"\0\0\xAA", timeout_ms)
    except (HidppError, OSError):
        return False
    return r is not None and len(r) >= 3 and r[2] == 0xAA


def feature_index(t, dev, feature, timeout_ms=500):
    r = request(t, dev, 0x00, 0, bytes([feature >> 8, feature & 0xFF]), timeout_ms)
    if r is None or r[0] == 0:
        return None
    return r[0]


def device_name(t, dev):
    idx = feature_index(t, dev, FEAT_DEVICE_NAME)
    if not idx:
        return None
    r = request(t, dev, idx, 0)
    if not r:
        return None
    length = r[0]
    name = b""
    while len(name) < length:
        r = request(t, dev, idx, 1, bytes([len(name)]))
        if not r:
            break
        chunk = r[: length - len(name)]
        if not chunk:
            break
        name += chunk
    return name.split(b"\0")[0].decode("utf-8", errors="replace")


def host_info(t, dev):
    """Return (number_of_channels, current_channel) with channels counted from 1."""
    idx = feature_index(t, dev, FEAT_CHANGE_HOST)
    if not idx:
        return None
    r = request(t, dev, idx, 0)
    if not r:
        return None
    return r[0], r[1] + 1


def switch_host(t, dev, channel):
    """Tell the device to jump to `channel` (1-based). The device leaves
    immediately, so no reply is expected."""
    idx = feature_index(t, dev, FEAT_CHANGE_HOST)
    if not idx:
        raise HidppError("no reply / no CHANGE_HOST feature")
    msg = bytes([REPORT_LONG, dev, idx, (1 << 4) | SW_ID, channel - 1]).ljust(LONG_LEN, b"\0")
    t.write(msg)


def _register_request(t, sub_id, register, params=(0, 0, 0), timeout_ms=500):
    """HID++ 1.0 register read/write on the receiver itself (index 0xFF)."""
    drain(t)
    msg = bytes([REPORT_SHORT, 0xFF, sub_id, register] + list(params))
    log.debug("    -> %s", msg.hex(" "))
    t.write(msg)
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            log.debug("    (no reply)")
            return None, []
        r = t.read(remaining)
        if not r or len(r) < 4:
            continue
        log.debug("    <- %s", r.hex(" "))
        if r[0] == REPORT_SHORT and r[1] == 0xFF and r[2] == sub_id and r[3] == register:
            return r[4:7], []
        if r[0] == REPORT_SHORT and r[1] == 0xFF and r[2] == 0x8F and r[3] == sub_id:
            raise HidppError(r[5] if len(r) > 5 else 0)


def receiver_links(t, debug_label=""):
    """Ask a receiver which paired devices currently have a live link.
    Returns {slot: True/False}, or None if the receiver didn't answer."""
    # make sure connection notifications are switched on (read-modify-write)
    try:
        flags, _ = _register_request(t, GET_REGISTER, REG_NOTIFICATIONS)
        if flags is not None and not (flags[1] & 0x01):
            _register_request(t, SET_REGISTER, REG_NOTIFICATIONS,
                              (flags[0], flags[1] | 0x01, flags[2]))
    except HidppError as e:
        log.debug("%s notifications register: error %s", debug_label, e)

    # ask the receiver to announce every paired device
    drain(t)
    t.write(bytes([REPORT_SHORT, 0xFF, SET_REGISTER, REG_CONNECTION, 0x02, 0, 0]))
    links = {}
    acked = False
    deadline = time.monotonic() + 0.6
    while True:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            break
        r = t.read(remaining)
        if r:
            log.debug("    <- %s", r.hex(" "))
        if not r or len(r) < 5 or r[0] != REPORT_SHORT:
            continue
        if r[1] == 0xFF and r[2] == SET_REGISTER and r[3] == REG_CONNECTION:
            acked = True
        elif r[2] == NOTIF_DEVICE_CONNECTION and 1 <= r[1] <= 6:
            links[r[1]] = not (r[4] & 0x40)  # bit 6 set = link not established
    log.debug("%s receiver links: %s (acked=%s)", debug_label, links, acked)
    return links if (acked or links) else None


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------

def scan(probe_timeout_ms=300):
    """Return [(path, dev_index, name)] for every HID++ 2.0 device that answers
    right now. Every handle opened here is closed again before returning."""
    seen = set()
    results = []
    for path in candidate_paths():
        log.debug("Probing %s", path)
        try:
            t = open_transport(path)
        except OSError as e:
            log.debug("  could not open: %s", e)
            continue
        try:
            for dev in DEVICE_INDICES:
                try:
                    r = request(t, dev, 0x00, 1, b"\0\0\xAA", probe_timeout_ms)
                    if r is None or len(r) < 3 or r[2] != 0xAA:
                        log.debug("  idx %#x: no reply", dev)
                        continue
                    name = device_name(t, dev) or "?"
                    log.debug("  idx %#x: found '%s'", dev, name)
                except HidppError as e:
                    log.debug("  idx %#x: error code %s", dev, e)
                    continue
                except OSError as e:
                    log.debug("  idx %#x: OS error %s", dev, e)
                    continue
                if name in seen:  # same device visible via two nodes (Linux)
                    continue
                seen.add(name)
                results.append((path, dev, name))
        finally:
            t.close()
    return results


class Device:
    """A device found by scan(). Opens a handle only while it is being used,
    so stale handles never pile up."""

    def __init__(self, path, dev, name):
        self.path, self.dev, self.name = path, dev, name
        self.short_path = None
        self.linux_name = None
        if not IS_WINDOWS and linux_bus(path) == "0005":
            # Bluetooth on Linux: the device node exists only while connected.
            # (Some Bluetooth devices answer on index 1 as well as 0xFF, so
            # decide by the bus, not by the index we happened to reach it on.)
            self.method = "bluetooth"
            self.linux_name = linux_uevent(path).get("HID_NAME")
        elif self.dev == 0xFF:
            self.method = "ping"
        else:
            if IS_WINDOWS:
                self.short_path = windows_short_path(path)
                log.debug("%s: receiver short channel = %s", name, self.short_path)
                self.method = "receiver" if self.short_path else "ping"
                if self.method == "receiver" and self.present() is None:
                    self.method = "ping"
            else:
                # On Linux the device may have been found through its own
                # per-device node rather than the receiver's; check the
                # receiver answers register queries on this node.
                self.short_path = path
                self.method = "receiver"
                if self.present() is None:
                    self.method = "ping"

    def __repr__(self):
        where = ("Bluetooth" if self.dev == 0xFF or self.method == "bluetooth"
                 else f"receiver slot {self.dev}")
        return f"{self.name} ({where}, presence check: {self.method})"

    def present(self):
        """True / False, or None if we couldn't tell this time."""
        if self.method == "bluetooth":
            for path in candidate_paths():
                if linux_bus(path) == "0005" and linux_uevent(path).get("HID_NAME") == self.linux_name:
                    return True
            return False
        if self.method == "receiver":
            try:
                t = open_transport(self.short_path)
            except OSError as e:
                log.debug("%s: could not open receiver channel %s: %s", self.name, self.short_path, e)
                return None
            try:
                links = receiver_links(t, self.name)
            except (HidppError, OSError) as e:
                log.debug("%s: receiver query failed: %r", self.name, e)
                links = None
            finally:
                t.close()
            if links is None:
                return None
            return links.get(self.dev, False)
        # fallback: ping (a sleeping device looks absent)
        try:
            t = open_transport(self.path)
        except OSError:
            return False
        try:
            return ping(t, self.dev)
        finally:
            t.close()

    def switch_to(self, channel):
        t = open_transport(self.path)
        try:
            switch_host(t, self.dev, channel)
        finally:
            t.close()

    def channels(self):
        t = open_transport(self.path)
        try:
            return host_info(t, self.dev)
        finally:
            t.close()


def find(name_part):
    for path, dev, name in scan():
        if name_part.lower() in name.lower():
            return Device(path, dev, name)
    return None


# --------------------------------------------------------------------------
# Idle guard (only used when we have to fall back to pinging)
# --------------------------------------------------------------------------

def idle_seconds():
    try:
        if IS_WINDOWS:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(lii)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            now = ctypes.windll.kernel32.GetTickCount()
            return ((now - lii.dwTime) & 0xFFFFFFFF) / 1000

        out = subprocess.run(
            ["gdbus", "call", "--session",
             "--dest", "org.gnome.Mutter.IdleMonitor",
             "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
             "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
            capture_output=True, text=True, timeout=2,
        ).stdout  # e.g. "(uint64 1234,)"
        digits = "".join(c for c in out.split()[-1] if c.isdigit())
        return int(digits) / 1000 if digits else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Desktop notifications
# --------------------------------------------------------------------------

_WIN_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:MF_TITLE)) > $null
$t.Item(1).AppendChild($x.CreateTextNode($env:MF_BODY)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def notify(args, level, title, body):
    """level 'info' or 'error'; --notify all|errors|off decides what's shown."""
    if args.notify == "off" or (args.notify == "errors" and level != "error"):
        return
    try:
        if IS_WINDOWS:
            env = dict(os.environ, MF_TITLE=title, MF_BODY=body)
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WIN_TOAST],
                env=env, creationflags=0x08000000,  # CREATE_NO_WINDOW
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(
                ["notify-send", "-a", "Mouse follow", "-i", "input-mouse",
                 "-t", "3000", title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.debug("Notification failed: %s", e)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def move_mouse(args, mouse):
    """Send the mouse away, retrying for a while in case it's asleep (it
    wakes as soon as you touch it)."""
    deadline = time.monotonic() + args.retry_seconds
    tried_once = False
    while time.monotonic() < deadline:
        if mouse is None:
            mouse = find(args.mouse)
        if mouse is not None:
            try:
                mouse.switch_to(args.mouse_target)
                tried_once = True
                log.debug("Sent change-host to %s", mouse.name)
            except (HidppError, OSError) as e:
                log.debug("Mouse didn't take the command (%s) - looking for it again", e)
                # the handle may be stale (e.g. a Bluetooth reconnect gave the
                # mouse a new device node on Linux) - rediscover next time round
                if isinstance(e, OSError):
                    mouse = None
                    continue
            time.sleep(1.0)
            gone = mouse.present()
            if (mouse.method != "ping" and gone is False) or \
               (mouse.method == "ping" and tried_once and not gone):
                log.info("Mouse moved to channel %d", args.mouse_target)
                notify(args, "info", "Mouse followed the keyboard",
                       f"Sent the mouse to channel {args.mouse_target}")
                return mouse
        else:
            time.sleep(1.0)
    log.info("Mouse didn't respond - it may be asleep (move it and press its "
             "channel button) or already on the other computer")
    notify(args, "error", "Mouse didn't follow",
           "Press the channel button under the mouse to switch it manually")
    return None


def run(args):
    keyboard = None
    while keyboard is None:
        keyboard = find(args.keyboard)
        if keyboard is None:
            log.info("Keyboard '%s' not found yet - make sure it's switched to this "
                     "computer and press a key to wake it", args.keyboard)
            time.sleep(5)
    log.info("Watching %s", keyboard)
    if keyboard.method == "ping" and not args.allow_ping:
        log.error("Can't check the keyboard's connection reliably here (the receiver "
                  "didn't answer the link-status query), and pinging it gives false "
                  "alarms whenever it dozes off. Not starting. Run with --list --debug "
                  "to see why, or add --allow-ping to run anyway.")
        sys.exit(1)

    mouse = find(args.mouse)
    if mouse:
        log.info("Mouse: %s -> will be sent to channel %d", mouse, args.mouse_target)
    else:
        log.info("Mouse '%s' not found yet (asleep?) - will look again when needed", args.mouse)

    was_present = True
    misses = 0
    while True:
        p = keyboard.present()
        log.debug("keyboard present: %s", p)
        if p is True:
            if not was_present:
                log.info("Keyboard is back on this computer")
            was_present = True
            misses = 0
            if mouse is None:
                mouse = find(args.mouse)
        elif p is False and was_present:
            misses += 1
            if misses >= 2:  # gone for two checks in a row
                was_present = False
                misses = 0
                idle = idle_seconds() if keyboard.method == "ping" else None
                if idle is not None and idle > args.idle_guard:
                    log.info("Keyboard stopped answering but you've been idle %.0fs "
                             "(probably asleep) - leaving the mouse alone", idle)
                else:
                    log.info("Keyboard left this computer - moving the mouse")
                    mouse = move_mouse(args, mouse)
        time.sleep(args.interval)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--keyboard", default="MX Keys", help="part of the keyboard's name")
    p.add_argument("--mouse", default="MX Master", help="part of the mouse's name")
    p.add_argument("--mouse-target", type=int,
                   help="mouse Easy-Switch channel (1-3) of the OTHER computer")
    p.add_argument("--idle-guard", type=int, default=120,
                   help="(ping fallback only) don't move the mouse if you've been idle "
                        "longer than this many seconds")
    p.add_argument("--retry-seconds", type=int, default=20,
                   help="keep trying to move a sleeping mouse for this long")
    p.add_argument("--interval", type=float, default=1.5, help="seconds between checks")
    p.add_argument("--list", action="store_true", help="list devices and their channels")
    p.add_argument("--test-switch", type=int, metavar="CH",
                   help="send the mouse to channel CH right now and exit")
    p.add_argument("--log-file", help="also write the log to this file")
    p.add_argument("--debug", action="store_true", help="show detailed output")
    p.add_argument("--notify", choices=("all", "errors", "off"), default="all",
                   help="desktop notifications: every switch, only failures, or none")
    p.add_argument("--test-notify", action="store_true",
                   help="show a test notification and exit")
    p.add_argument("--allow-ping", action="store_true",
                   help="run even if the keyboard can only be checked by pinging "
                        "(unreliable: a dozing keyboard looks disconnected)")
    args = p.parse_args()

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        handlers=handlers, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.test_notify:
        notify(args, "error", "Mouse follow", "Notifications are working")
        time.sleep(2)  # give the notifier a moment before we exit
        return

    if args.list:
        found = False
        for path, dev, name in scan():
            found = True
            d = Device(path, dev, name)
            try:
                info = d.channels()
            except (HidppError, OSError):
                info = None
            ch = f"on channel {info[1]} of {info[0]}" if info else "no Easy-Switch info"
            print(f"{d}  ->  {ch}  (connected now: {d.present()})")
        if not found:
            print("No Logitech HID++ devices answered. Wake them by pressing a key / "
                  "moving the mouse and try again." +
                  (" If that doesn't help, quit Logi Options+." if IS_WINDOWS else
                   " Also check permissions on /dev/hidraw* (install the 'solaar' package)."))
        return

    if args.test_switch:
        m = find(args.mouse)
        if not m:
            sys.exit(f"Mouse matching '{args.mouse}' not found - move it to wake it and retry")
        m.switch_to(args.test_switch)
        print(f"Sent {m.name} to channel {args.test_switch}")
        return

    if not args.mouse_target:
        p.error("--mouse-target is required (the mouse's channel for the other computer)")
    try:
        run(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

