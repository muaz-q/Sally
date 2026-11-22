"""The `sally` command.

    sally                 start Sally (signs in to Spotify the first time)
    sally stop            quit and put your own wallpaper back
    sally status          what's playing, live mode, battery
    sally live on|off     the animated rings, controls, lyrics and spiral
    sally battery on|off  rest live mode on battery saver / low battery  [--below 20]
    sally autostart on|off
    sally login | logout  [--client-id ID]
    sally logs            open the log file
    sally uninstall       stop, remove autostart and Sally's files
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

from . import __version__

ON_OFF = {"on": True, "off": False}


def say(text):
    print(text, flush=True)


def running():
    from . import control
    return control.send("status")


def change(key, value):
    """Change a setting in the running app, or in settings.json when Sally isn't running."""
    from . import config, control
    if control.send(("set", key, value)) is None:
        settings = config.load()
        settings[key] = value
        config.save(settings)
        if key == "autostart":
            from .app import set_autostart
            set_autostart(value)
        return False
    return True


def show_link(url):
    say("If no browser tab opened, open this link yourself:")
    say(f"  {url}")
    say("Waiting for Spotify...")


def pythonw():
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.exists(exe) else sys.executable


# ── commands ─────────────────────────────────
def cmd_start(args):
    from . import auth, paths
    paths.ensure()
    if running():
        say("Sally is already running. Look for its icon in the taskbar tray.")
        return 0
    if not auth.logged_in():
        say("First, sign in to Spotify. Your browser will open: click Agree, then come back here.")
        try:
            auth.login(show_url=show_link)
        except Exception as e:
            say(f"Spotify sign-in did not finish: {e}")
            return 1
        say("Signed in.")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen([pythonw(), "-m", "sally", "run"], creationflags=flags, close_fds=True,
                     cwd=str(paths.DATA), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        if running():
            say("Sally is running. It lives in the taskbar tray; `sally stop` quits it.")
            return 0
    say(f"Sally did not answer. Check the log: {paths.LOG}")
    return 1


def cmd_run(args):
    from .app import run
    return run()


def cmd_stop(args):
    from . import control
    if control.send(("quit",)) is None:
        say("Sally isn't running.")
        return 0
    for _ in range(20):
        time.sleep(0.25)
        if not running():
            break
    say("Sally stopped and your own wallpaper is back.")
    return 0


def cmd_status(args):
    from . import auth, config, power
    s = running()
    battery = power.read()
    if battery["percent"] is not None:
        source = "on battery" if battery["on_battery"] else "plugged in"
        saver = ", battery saver on" if battery["saver"] else ""
        battery_text = f"{battery['percent']}% ({source}{saver})"
    else:
        battery_text = "no battery"
    if s is None:
        settings = config.load()
        say("Sally:     not running (start it with `sally`)")
        say(f"Live mode: {'on' if settings['live_mode'] else 'off'}")
        say(f"Battery:   {battery_text}")
        say(f"Spotify:   {'signed in' if auth.logged_in() else 'not signed in'}")
        return 0
    say("Sally:     running")
    say(f"Playing:   {s['track'] + ('' if s['playing'] else ' (paused)') if s['track'] else 'nothing'}")
    if not s["live_mode"]:
        live = "off"
    elif s["resting"]:
        live = f"resting, because the {s['resting']}"
    else:
        live = "on"
    say(f"Live mode: {live}")
    rule = f"rests below {s['battery_threshold']}% or on battery saver" if s["battery_auto"] else "never rests"
    say(f"Battery:   {battery_text}; live mode {rule}")
    return 0


def cmd_live(args):
    value = ON_OFF[args.state]
    live = change("live_mode", value)
    word = "on" if value else "off (plain wallpaper, almost no battery use)"
    say(f"Live mode {word}." + ("" if live else " It applies the next time Sally starts."))
    return 0


def cmd_battery(args):
    value = ON_OFF[args.state]
    if args.below is not None:
        if not 1 <= args.below <= 100:
            say("--below takes a percentage from 1 to 100.")
            return 2
        change("battery_threshold", args.below)
    change("battery_auto", value)
    if value:
        from . import config
        below = args.below if args.below is not None else config.load()["battery_threshold"]
        say(f"Live mode will rest on battery saver, or below {below}% while unplugged.")
    else:
        say("Live mode will stay on whatever the battery does.")
    return 0


def cmd_autostart(args):
    value = ON_OFF[args.state]
    change("autostart", value)
    say("Sally will start with Windows." if value else "Sally won't start with Windows.")
    return 0


def cmd_login(args):
    from . import auth, config
    if args.client_id is not None:
        settings = config.load()
        settings["spotify_client_id"] = args.client_id.strip()
        config.save(settings)
        auth.logout()  # a token from another app can't be refreshed with this one
    if auth.logged_in():
        say("Already signed in to Spotify. `sally logout` signs out.")
        return 0
    say("Your browser will open: click Agree, then come back here.")
    try:
        auth.login(show_url=show_link)
    except Exception as e:
        say(f"Spotify sign-in did not finish: {e}")
        return 1
    say("Signed in to Spotify.")
    return 0


def cmd_logout(args):
    from . import auth
    cmd_stop(args)
    auth.logout()
    say("Signed out of Spotify.")
    return 0


def cmd_history(args):
    import ctypes
    from . import control
    ctypes.windll.user32.AllowSetForegroundWindow(-1)   # ASFW_ANY: let Sally come to the front
    if control.send(("dashboard",)) is None:
        say("Sally isn't running. Start it with `sally`, then run `sally history` again.")
        return 1
    say("Opened your listening history.")
    return 0


def cmd_logs(args):
    from . import paths
    if not paths.LOG.exists():
        say("No log yet.")
        return 0
    os.startfile(paths.LOG)
    say(str(paths.LOG))
    return 0


def cmd_uninstall(args):
    from . import paths
    if not args.yes:
        answer = input(f"Stop Sally, remove it from startup and delete {paths.DATA}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            say("Nothing changed.")
            return 0
    cmd_stop(args)
    from .app import set_autostart
    set_autostart(False)
    shutil.rmtree(paths.DATA, ignore_errors=True)
    say("Done. To remove the program itself: pipx uninstall sally-wallpaper")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="sally", description="Your Spotify cover as a live Windows wallpaper.")
    p.add_argument("--version", action="version", version=f"sally {__version__}")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("start", help="start Sally (the default)").set_defaults(func=cmd_start)
    sub.add_parser("run", help="run in the foreground (used at login)").set_defaults(func=cmd_run)
    sub.add_parser("stop", help="quit and restore your wallpaper").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="what's playing, live mode, battery").set_defaults(func=cmd_status)
    live = sub.add_parser("live", help="turn the animated setup on or off")
    live.add_argument("state", choices=ON_OFF)
    live.set_defaults(func=cmd_live)
    battery = sub.add_parser("battery", help="rest live mode on battery saver / low battery")
    battery.add_argument("state", choices=ON_OFF)
    battery.add_argument("--below", type=int, metavar="PERCENT", help="battery level that counts as low (default 20)")
    battery.set_defaults(func=cmd_battery)
    auto = sub.add_parser("autostart", help="start Sally with Windows")
    auto.add_argument("state", choices=ON_OFF)
    auto.set_defaults(func=cmd_autostart)
    login = sub.add_parser("login", help="sign in to Spotify")
    login.add_argument("--client-id", help="use your own Spotify app instead of Sally's")
    login.set_defaults(func=cmd_login)
    sub.add_parser("logout", help="sign out of Spotify").set_defaults(func=cmd_logout)
    sub.add_parser("history", help="open the listening-history dashboard").set_defaults(func=cmd_history)
    sub.add_parser("logs", help="open the log file").set_defaults(func=cmd_logs)
    uninstall = sub.add_parser("uninstall", help="stop Sally and remove its settings and startup entry")
    uninstall.add_argument("--yes", action="store_true", help="don't ask first")
    uninstall.set_defaults(func=cmd_uninstall)
    return p


def main(argv=None):
    if sys.platform != "win32":
        print("Sally only runs on Windows for now.", file=sys.stderr)
        return 1
    args = parser().parse_args(argv)
    func = getattr(args, "func", cmd_start)
    try:
        return func(args) or 0
    except KeyboardInterrupt:
        return 130
