"""How the `sally` command talks to the running app: a named pipe for this Windows user,
answered only by clients that know the random key in the user's own Sally folder."""
import getpass
import re
import secrets
import threading
from multiprocessing.connection import Client, Listener

from . import paths

ADDRESS = r"\\.\pipe\sally-" + re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser())


def key():
    try:
        return paths.CONTROL_KEY.read_bytes()
    except FileNotFoundError:
        paths.DATA.mkdir(parents=True, exist_ok=True)
        value = secrets.token_bytes(32)
        paths.CONTROL_KEY.write_bytes(value)
        return value


def send(message):
    """The app's reply, or None when Sally is not running."""
    try:
        with Client(ADDRESS, family="AF_PIPE", authkey=key()) as conn:
            conn.send(message)
            return conn.recv()
    except (OSError, EOFError):
        return None


def serve(handler, log):
    """Answer messages on a background thread; handler(message) -> reply."""
    def loop():
        try:
            listener = Listener(ADDRESS, family="AF_PIPE", authkey=key())
        except OSError as e:
            log.warning(f"Command pipe unavailable: {e}")
            return
        while True:
            try:
                with listener.accept() as conn:
                    conn.send(handler(conn.recv()))
            except Exception as e:  # a bad key or a client that hung up: keep serving
                log.info(f"Command pipe: {type(e).__name__}")
    threading.Thread(target=loop, daemon=True, name="control").start()
