"""Start the installed achilterm command and exercise it over HTTP."""

import contextlib
import functools
import gzip
import hashlib
import os
import random
import re
import resource
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from importlib.metadata import version
from xml.etree import ElementTree

import webob

from achilterm.achilterm import AchilTerm, Terminal

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


def check_escape_sequences():
    """No sequence may move the cursor or scroll region off the screen."""
    # A zero or missing row means the first or last, and a region whose top
    # isn't above its bottom is ignored.
    for sequence, region in (
        ("[r", (0, 24)),
        ("[0;0r", (0, 24)),
        ("[1;0r", (0, 24)),
        ("[3r", (2, 24)),
        ("[5;20r", (4, 19)),
        ("[20;5r", (1, 9)),
        ("[99;99r", (1, 9)),
    ):
        term = Terminal(80, 25)
        term.write(f"\x1b[2;10r\x1b{sequence}".encode())
        assert (term.st, term.sb) == region, (sequence, term.st, term.sb)

    # Lines and characters are inserted and deleted within the region and row.
    for sequence, rows in (
        ("[2;1H\x1b[L", "abcd ____ efgh ijkl"),
        ("[2;1H\x1b[2L", "abcd ____ ____ efgh"),
        ("[2;1H\x1b[999L", "abcd ____ ____ ____"),
        ("[2;1H\x1b[2M", "abcd mnop ____ ____"),
        ("[2;1H\x1b[999M", "abcd ____ ____ ____"),
        ("[2;3H\x1b[@", "abcd ef_g ijkl mnop"),
        ("[2;3H\x1b[999@", "abcd ef__ ijkl mnop"),
        ("[2;2H\x1b[X", "abcd e_gh ijkl mnop"),
        ("[2;2H\x1b[999X", "abcd e___ ijkl mnop"),
    ):
        term = Terminal(4, 4)
        term.write(f"abcdefghijklmnop\x1b{sequence}".encode())
        text = "".join(chr(cell & 0xFFFFFF) for cell in term.scr).replace(" ", "_")
        shown = " ".join(text[start : start + 4] for start in range(0, 16, 4))
        assert shown == rows, (sequence, shown)

    numbers = ("", "0", "1", "3", "25", "26", "80", "81", "999")
    parts = ["a", "bbbbbbbbbbbb", "\n", "\r", "\t", "\x08", "\u20ac"]
    parts += ["\x1b7", "\x1b8", "\x1bM", "\x1bc"]
    finals = "@ABCDEFGHJKLMPXacdefhlmrsu`"
    rng = random.Random(1)
    # sequences that have caused trouble, then random ones
    cases = [["\x1b[0;0r\x1b[0d\n"], ["\x1b[r"] + ["a\n"] * 40, ["\x1b[0;0H\x1b[K"]]
    cases += [["\x1b[5;80H\x1b[9@"], ["\x1b[25;80H\x1b[999X"]]
    for trial in range(3000):
        case = []
        for step in range(20):
            if rng.choice((True, False)):
                case.append(rng.choice(parts))
            else:
                values = ";".join(rng.choices(numbers, k=rng.randrange(4)))
                case.append(f"\x1b[{values}{rng.choice(finals)}")
        cases.append(case)
    for case in cases:
        for width, height in ((3, 3), (80, 25)):
            term = Terminal(width, height)
            for text in case:
                term.write(text.encode())
                assert len(term.scr) == width * height, case
                assert 0 <= term.cx < width and 0 <= term.cy < height, case
                assert 0 <= term.st <= term.sb < height, case
            ElementTree.fromstring(term.dumphtml().encode())


def post(app, name, keys=""):
    """Ask an application running here for a terminal's screen."""
    query = terminal_query(name, keys)
    request = webob.Request.blank("/u", headers=PAGE_HEADERS, POST=query)
    return request.get_response(app)


@contextlib.contextmanager
def running_app(*args, **options):
    app = AchilTerm(*args, **options)
    try:
        yield app
    finally:
        app.multi.die()
        app.multi.thread.join(10)


def wait_for_screen(send, keys, pattern):
    """Send keys, then wait until the screen matches the pattern."""
    for attempt in range(50):
        # A response only holds the screen when it has changed, so check every
        # one, including the response to the keys.
        screen = send(keys if attempt == 0 else "")
        if "<pre" in screen and re.search(pattern, screen_text(screen)):
            return screen
        time.sleep(0.1)
    raise AssertionError(f"{pattern!r} never appeared: {screen}")


def check_expiry():
    # Runs the application here, to use a timeout shorter than the command allows.
    with running_app("while :; do echo busy; sleep 1; done", timeout=2) as app:
        pids = {}
        for name in ("shown", "left"):
            assert post(app, name).status_code == 200
            key = app.session[session_id(name)]
            pids[name] = app.multi.proc[key]["pid"]
        # A terminal closes once no page asks for it, even while it has output,
        # and stays open while one does.
        for attempt in range(50):
            time.sleep(0.2)
            assert post(app, "shown").status_code == 200
            # Go on past the timeout, so that "shown" has to outlast it.
            if attempt > 12 and not is_running(pids["left"]):
                break
        assert is_running(pids["shown"]), "a shown terminal was closed"
        assert not is_running(pids["left"]), "an abandoned terminal stayed open"
        # Its page is told, every time, and gets no new terminal.
        for attempt in range(2):
            response = post(app, "left")
            assert response.status_code == 410, response.status
            assert "disconnected. Reload" in response.text, response.text
        assert len(app.multi.proc) == 1, app.multi.proc


def check_output_fault():
    """If one terminal's output can't be handled, only that one is closed."""

    def fail(*args):
        raise RuntimeError("deliberate failure for this test")

    with running_app("while :; do echo busy; sleep 1; done") as app:
        assert post(app, "other").status_code == 200
        # reading the output, then drawing the screen
        for method in ("write", "dumphtml"):
            assert post(app, method).status_code == 200
            key = app.session[session_id(method)]
            # The error is expected, so collect its log entry instead of showing it.
            with unittest.TestCase().assertLogs("achilterm.achilterm") as logs:
                setattr(app.multi.proc[key]["term"], method, fail)
                for attempt in range(50):
                    time.sleep(0.2)
                    response = post(app, method)
                    if response.status_code != 200:
                        break
            assert response.status_code == 410, response.status
            assert "server error" in response.text, response.text
            errors = [record.exc_info[0] for record in logs.records]
            assert errors == [RuntimeError], errors
        assert post(app, "other").status_code == 200
        assert app.multi.thread.is_alive()


def check_many_descriptors():
    """Terminals must work when their descriptors are numbered 1024 or higher."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 2048 if hard == resource.RLIM_INFINITY else min(2048, hard)
    if wanted < 1100:
        print(f"skipping the descriptor check: the open file limit is {hard}")
        return
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, wanted), hard))
    filler = [os.open(os.devnull, os.O_RDONLY)]
    try:
        while filler[-1] < 1024:
            filler.append(os.open(os.devnull, os.O_RDONLY))
        with running_app('read line; echo "got $line"; sleep 30') as app:

            def send(keys):
                response = post(app, "high", keys)
                response.decode_content()
                return response.text

            wait_for_screen(send, "input\r", "got input")
            assert min(proc["fd"] for proc in app.multi.proc.values()) >= 1024
    finally:
        for fd in filler:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


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
        send = functools.partial(terminal, session)
        return wait_for_screen(send, keys, pattern)

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
    check_escape_sequences()
    check_expiry()
    check_output_fault()
    check_many_descriptors()
    print("smoke test passed")


if __name__ == "__main__":
    main()
