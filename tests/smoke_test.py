"""Start the installed achilterm command and exercise it over HTTP."""

import gzip
import hashlib
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

import webob

from achilterm.achilterm import AchilTerm

COMMAND = os.path.join(os.path.dirname(sys.executable), "achilterm")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# The header the terminal page sends with its requests.
PAGE_HEADERS = {"X-Achilterm": "1"}


def fetch(url, data=None, headers=PAGE_HEADERS):
    request = urllib.request.Request(url, data, headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body.decode()


def status(url, data=None, headers=PAGE_HEADERS):
    try:
        fetch(url, data, headers)
    except urllib.error.HTTPError as error:
        return error.code
    return 200


def session_id(name):
    return hashlib.sha256(name.encode()).hexdigest()[:32]


def terminal_query(name, keys="", **extra):
    fields = {"s": session_id(name), "w": 80, "h": 25, "c": 1, "k": keys, **extra}
    return urllib.parse.urlencode(fields).encode()


def is_running(pid):
    try:
        with open(f"/proc/{pid}/stat") as stat:
            # An ended process stays as a zombie until it is collected.
            return stat.read().rpartition(")")[2].split()[0] != "Z"
    except FileNotFoundError:
        return False


def start_server(*options):
    port = free_port()
    base = f"http://127.0.0.1:{port}/"
    server = subprocess.Popen(
        [COMMAND, f"--port={port}", "--command=exec /bin/sh", *options],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for attempt in range(50):
        try:
            fetch(base)
            return server, port, base
        except OSError:
            time.sleep(0.1)
    server.kill()
    raise AssertionError("server did not start")


def check_access():
    server, port, base = start_server("--allow-host=Terminal.Example.com:443")
    url = f"{base}u"
    try:
        # Only requests from the terminal page, with a random session ID.
        assert status(url, terminal_query("a"), headers={}) == 403
        for bad in ("", "1", "123456789", "g" * 32, "a" * 33):
            query = urllib.parse.urlencode({"s": bad, "k": ""}).encode()
            assert status(url, query) == 400, bad

        # Only for localhost and the names that were allowed.
        for host, expected in (
            (f"localhost:{port}", 200),
            ("[::1]", 200),
            ("terminal.example.com", 200),
            ("attacker.example", 403),
            ("localhost.attacker.example", 403),
            ("", 403),
        ):
            headers = {**PAGE_HEADERS, "Host": host}
            assert status(base, headers=headers) == expected, host
            assert status(url, terminal_query("h"), headers) == expected, host

        # A page left over from before a restart gets no terminal, and is told why
        # in plain text.
        stale = terminal_query("restart", i="0123456789abcdef")
        try:
            fetch(url, stale)
        except urllib.error.HTTPError as error:
            assert error.code == 410, error.code
            assert error.headers.get_content_type() == "text/plain", error.headers
            assert b"restarted" in error.read()
        else:
            raise AssertionError("a page from before a restart was accepted")
        request = urllib.request.Request(url, terminal_query("restart"), PAGE_HEADERS)
        with urllib.request.urlopen(request, timeout=10) as response:
            instance = response.headers["X-Achilterm-Instance"]
        assert status(url, terminal_query("restart", i=instance)) == 200

        # The whole screen is sent again when asked for, even if unchanged.
        time.sleep(1)
        fetch(url, terminal_query("restart"))
        assert "<idem>" in fetch(url, terminal_query("restart"))
        assert "<pre" in fetch(url, terminal_query("restart", f=1))

        # Other websites may not frame the page.
        with urllib.request.urlopen(base, timeout=10) as response:
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    finally:
        server.terminate()
        errors = server.communicate(timeout=10)[1].decode()
    assert "Traceback" not in errors, errors

    for option in (
        "--timeout=89",
        "--allow-host=[nonsense",
        "--allow-host=https://terminal.example.com",
        "--allow-host=*.example.com",
        "--allow-host=a.example,b.example",
    ):
        result = subprocess.run(
            [COMMAND, option], capture_output=True, timeout=10, check=False
        )
        assert result.returncode == 2, (option, result)


def check_expiry():
    # Runs the application here, to use a timeout shorter than the command allows.
    app = AchilTerm("while :; do echo busy; sleep 1; done", timeout=2)

    def post(name):
        request = webob.Request.blank(
            "/u", headers=PAGE_HEADERS, POST=terminal_query(name)
        )
        return request.get_response(app)

    try:
        pids = {}
        for name in ("shown", "left"):
            assert post(name).status_code == 200
            key = app.session[session_id(name)]
            pids[name] = app.multi.proc[key]["pid"]
        # A terminal closes once no page asks for it, even while it has output,
        # and stays open while one does.
        for attempt in range(50):
            time.sleep(0.2)
            assert post("shown").status_code == 200
            # Go on past the timeout, so that "shown" has to outlast it.
            if attempt > 12 and not is_running(pids["left"]):
                break
        assert is_running(pids["shown"]), "a shown terminal was closed"
        assert not is_running(pids["left"]), "an abandoned terminal stayed open"
        # Its page is told, every time, and gets no new terminal.
        for attempt in range(2):
            response = post("left")
            assert response.status_code == 410, response.status
            assert "Reload" in response.text
        assert len(app.multi.proc) == 1, app.multi.proc
    finally:
        app.multi.die()
        app.multi.thread.join(10)


def screen_text(response):
    return "".join(ElementTree.fromstring(response.encode()).itertext())


def main():
    reported = subprocess.run(
        [COMMAND, "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert version("achilterm-ng") in reported, reported

    server, port, base = start_server()

    def terminal(session, keys=""):
        # POST, as the browser does, so that large input fits.
        response = fetch(f"{base}u", terminal_query(session, keys))
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
        index = fetch(base)
        assert "github.com/paulschreiber/achilterm" in index, "no source link"
        for name in ("achilterm.js", "achilterm.css"):
            assert fetch(base + name), name

        # Keystrokes must not be accepted in an address, which may be logged.
        assert status(f"{base}u?s=get&w=80&h=25&c=1&k=x") == 405

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
    check_access()
    check_expiry()
    print("smoke test passed")


if __name__ == "__main__":
    main()
