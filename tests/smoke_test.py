"""Start the installed achilterm command and exercise it over HTTP."""

import gzip
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from importlib.metadata import version
from xml.etree import ElementTree

COMMAND = os.path.join(os.path.dirname(sys.executable), "achilterm")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(url, data=None):
    with urllib.request.urlopen(url, data, timeout=10) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body.decode()


def screen_text(response):
    return "".join(ElementTree.fromstring(response.encode()).itertext())


def main():
    reported = subprocess.run(
        [COMMAND, "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert version("achilterm-ng") in reported, reported

    port = free_port()
    base = f"http://127.0.0.1:{port}/"
    server = subprocess.Popen(
        [COMMAND, f"--port={port}", "--command=exec /bin/sh"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def terminal(session, keys=""):
        query = urllib.parse.urlencode(
            {"s": session, "w": 80, "h": 25, "c": 1, "k": keys}
        )
        # POST, as the browser does, so that large input fits.
        response = fetch(f"{base}u", query.encode())
        # The browser can't update the screen from invalid XML.
        ElementTree.fromstring(response.encode())
        return response

    def expect(session, keys, pattern):
        """Send keys, then wait until the screen matches the pattern."""
        for attempt in range(50):
            # A response only holds the screen when it has changed, so check
            # every one, including the response to the keys.
            screen = terminal(session, keys if attempt == 0 else "")
            if re.search(pattern, screen_text(screen)):
                return screen
            time.sleep(0.1)
        raise AssertionError(f"{pattern!r} never appeared: {screen}")

    try:
        for attempt in range(50):
            try:
                index = fetch(base)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("server did not start")
        assert "github.com/paulschreiber/achilterm" in index, "no source link"
        for name in ("achilterm.js", "achilterm.css"):
            assert fetch(base + name), name

        # Keystrokes must not be accepted in an address, which may be logged.
        try:
            fetch(f"{base}u?s=get&w=80&h=25&c=1&k=x")
        except urllib.error.HTTPError as error:
            assert error.code == 405, error.code
        else:
            raise AssertionError("a GET request was accepted")

        # A tab whose shell has ended must never reach a newer terminal. This
        # runs first, while the next terminal would reuse the ended one's file
        # descriptor.
        terminal("stale")
        terminal("stale", "exit\r")
        time.sleep(1)
        terminal("newer")
        expect("newer", "echo private$((20+1))\r", "private21")
        screen = terminal("stale", "\x15echo intruder$((30+1))\r")
        assert "private21" not in screen_text(screen), screen_text(screen)
        time.sleep(1)
        screen = terminal("newer", " ")
        assert "intruder31" not in screen_text(screen), screen_text(screen)
        terminal("newer", "\x15exit\r")

        # An idle connection must not block other requests.
        with socket.create_connection(("127.0.0.1", port)):
            terminal("first")
            terminal("second")

            # Ending one session must leave the others working.
            terminal("first", "exit\r")
            time.sleep(1)
            terminal("first")
            expect("second", "echo marker$((40+2))\r", "marker42")
            terminal("third")

            # A large paste must arrive complete.
            terminal(
                "third", "stty raw -echo; echo ready; head -c $((50000*2)) | wc -c\r"
            )
            time.sleep(1)
            expect("third", "x" * 100000, "100000")

            # Split characters, invalid bytes and terminal queries in the output
            # must not stop screen updates for any session.
            terminal("fourth")
            expect(
                "fourth",
                "printf '\\303'; sleep 0.5; printf '\\251 \\377\\n'; echo ok$((5+5))\r",
                "ok10",
            )
            expect(
                "fourth",
                "stty raw -echo; printf '\\033[c'; head -c 5 | od -An -c; stty sane\r",
                r"\[\s+\?\s+6\s+c",
            )
            expect("second", "echo still$((6+6))\r", "still12")

            # Characters beyond Latin-1 must appear as written, in the usual
            # colors, and control characters must not break the response.
            terminal("fifth")
            text = "\u00e9\u20ac\u03bb\u0416\u2500\u4e2d\U0001f600"
            # C0 and C1 controls, DEL, a noncharacter, a direction override and
            # a line separator.
            controls = "\x01\x02\x0e\x7f\x85\ufffe\u202e\u2028"
            octal = "".join(f"\\{byte:03o}" for byte in (controls + text).encode())
            screen = expect(
                "fifth",
                f"printf '<{octal}>\\n'; echo end$((7+7))\r",
                "end14",
            )
            assert f"<{text}>" in screen_text(screen), screen
            colors = set(re.findall(r'class="f(\d+) b(\d+)"', screen))
            assert colors <= {("7", "0"), ("7", "1")}, colors
    finally:
        server.terminate()
        errors = server.communicate(timeout=10)[1].decode()

    assert "Traceback" not in errors, errors
    print("smoke test passed")


if __name__ == "__main__":
    main()
