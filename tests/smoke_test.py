"""Start the installed achilterm command and exercise it over HTTP."""

import gzip
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from importlib.metadata import version

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
        return fetch(f"{base}u", query.encode())

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

        # An idle connection must not block other requests.
        with socket.create_connection(("127.0.0.1", port)):
            terminal("first")
            terminal("second")

            # Ending one session must leave the others working.
            terminal("first", "exit\r")
            time.sleep(1)
            terminal("first")
            terminal("second", "echo marker$((40+2))\r")
            time.sleep(1)
            screen = terminal("second", " ")
            assert "marker42" in screen, screen
            terminal("third")

            # A large paste must arrive complete.
            terminal(
                "third", "stty raw -echo; echo ready; head -c $((50000*2)) | wc -c\r"
            )
            time.sleep(1)
            terminal("third", "x" * 100000)
            for attempt in range(50):
                time.sleep(0.1)
                screen = terminal("third", " ")
                if "100000" in re.sub(r"<[^>]+>", "", screen):
                    break
            else:
                raise AssertionError(f"large input was cut off: {screen}")
    finally:
        server.terminate()
        errors = server.communicate(timeout=10)[1].decode()

    assert "Traceback" not in errors, errors
    print("smoke test passed")


if __name__ == "__main__":
    main()
