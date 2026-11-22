"""Spotify sign-in with PKCE: the browser asks the user to agree, and no secret is needed,
so the app ID below can ship with Sally. People who made their own Spotify app can use
theirs instead with `sally login --client-id ...`."""
import os
import webbrowser

from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyOauthError, SpotifyStateError
from spotipy.oauth2 import SpotifyPKCE, start_local_http_server

from . import config, paths

CLIENT_ID = "a78d6774e02f4f3698c386f48627ecbb"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
# reading what's playing, controlling playback, and your top / recent tracks for the spiral.
# Adding scopes makes Spotify ask for permission once more, in the browser.
SCOPES = " ".join([
    "user-read-currently-playing", "user-read-playback-state", "user-modify-playback-state",
    "user-top-read", "user-read-recently-played",
])


def client_id():
    return os.environ.get("SALLY_SPOTIFY_CLIENT_ID") or config.load()["spotify_client_id"] or CLIENT_ID


SIGN_IN_TIMEOUT = 300  # seconds to wait for the browser to come back


class SignInNeeded(Exception):
    """No usable token, and this process must not open a browser on its own."""


class _PKCE(SpotifyPKCE):
    interactive = False   # only `sally login` / `sally start` may open the browser
    show_url = None       # called with the sign-in link, for people whose browser stays hidden

    def _open_auth_url(self, state=None):
        url = self.get_authorize_url(state)
        if self.show_url:
            self.show_url(url)
        webbrowser.open(url)

    def _get_auth_response(self, open_browser=None):
        if not self.interactive:
            raise SignInNeeded("Not signed in to Spotify. Run: sally login")
        server = start_local_http_server(int(REDIRECT_URI.rsplit(":", 1)[1].split("/")[0]))
        server.timeout = SIGN_IN_TIMEOUT   # stop waiting eventually instead of hanging forever
        try:
            self._open_auth_url()
            server.handle_request()
        finally:
            server.server_close()
        if self.state is not None and server.state != self.state:
            raise SpotifyStateError(self.state, server.state)
        if server.auth_code is not None:
            return server.auth_code
        if server.error is not None:
            raise SpotifyOauthError(f"Spotify said: {server.error}")
        raise SpotifyOauthError("the browser never came back (timed out)")


def manager(interactive=False, show_url=None):
    paths.DATA.mkdir(parents=True, exist_ok=True)
    m = _PKCE(client_id=client_id(), redirect_uri=REDIRECT_URI, scope=SCOPES,
              cache_handler=CacheFileHandler(cache_path=str(paths.TOKEN)), open_browser=True)
    m.interactive, m.show_url = interactive, show_url
    return m


def logged_in():
    token = manager().cache_handler.get_cached_token()
    return bool(token and token.get("refresh_token")
                and set(SCOPES.split()) <= set((token.get("scope") or "").split()))


def login(show_url=None):
    """Opens the browser and waits for Spotify to send the user back to 127.0.0.1:8888."""
    manager(interactive=True, show_url=show_url).get_access_token(check_cache=True)


def logout():
    try:
        paths.TOKEN.unlink()
    except FileNotFoundError:
        pass
