# -*- coding: utf-8 -*-
"""
A tiny, dependency-free HTTP server exposed as an IPython extension.

Once loaded inside an IPython/Jupyter session it starts a background HTTP
server (using only the Python standard library) that lets you, from any HTTP
client:

- fetch the input history of the running session,
- execute code inside the live kernel namespace,
- read the value (``repr``) of variables from the user namespace.

Remote executions are made *visible* in the live session: by default the
submitted code is stored in the interactive ``In``/``Out`` history
(``execution_count`` advances just like a normal cell) and is echoed to the
real terminal so a human watching the session sees exactly what ran. Every
request is also logged through the standard ``logging`` module.

Usage
-----
In an IPython session::

    %load_ext pytexit.ipython_http

By default the server listens on ``127.0.0.1:8899``. Behaviour can be tuned
through environment variables *before* loading the extension:

- ``PYTEXIT_HTTP_HOST``           bind host           (default ``127.0.0.1``)
- ``PYTEXIT_HTTP_PORT``           bind port           (default ``8899``)
- ``PYTEXIT_HTTP_ECHO``           echo code to the terminal, ``0``/``1`` (default ``1``)
- ``PYTEXIT_HTTP_STORE_HISTORY``  record remote code in ``In``/``Out``, ``0``/``1`` (default ``1``)

To stop the server::

    %unload_ext pytexit.ipython_http

Endpoints
---------
``GET  /``                  Small help / index of available endpoints.
``GET  /history``           Return the session input history.
                            Optional query params: ``raw`` (0/1, default 1),
                            ``limit`` (int, number of most recent entries).
``GET  /variables``         List the names of user-defined variables.
``GET  /variable/<name>``   Return the ``repr`` (and type) of a single variable.
``POST /execute``           Execute code. Body may be raw source code, or a
                            JSON object ``{"code": "..."}``. Returns stdout,
                            the ``repr`` of the last expression, and any error.

Security
--------
This server executes arbitrary code and exposes the whole namespace with **no
authentication**. It binds to ``127.0.0.1`` by default. Do *not* expose it on a
public interface on an untrusted network.
"""

from __future__ import absolute_import, print_function

import io
import json
import logging
import os
import sys
import threading
import traceback

try:  # Python 3
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import parse_qs, unquote, urlparse
except ImportError:  # Python 2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn
    from urlparse import parse_qs, urlparse
    from urllib import unquote


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899

logger = logging.getLogger("pytexit.ipython_http")

# Module level handle to the running server, so the extension can be unloaded.
_SERVER = None


def _env_flag(name, default=True):
    """Read a boolean-ish environment variable."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread."""

    daemon_threads = True
    allow_reuse_address = True


def _echo(text):
    """Write ``text`` to the *real* terminal, bypassing our stdout capture.

    ``sys.__stdout__`` is the interpreter's original stdout, so this is visible
    to a human watching the IPython terminal even while we have temporarily
    redirected ``sys.stdout`` to capture the execution output.
    """
    stream = sys.__stdout__
    if stream is None:  # e.g. a detached kernel with no console
        return
    try:
        stream.write(text)
        stream.flush()
    except Exception:  # pragma: no cover - never let echoing break execution
        pass


def _capture_execution(shell, code, store_history=True, echo=True):
    """Run ``code`` in the IPython ``shell`` and capture stdout/stderr.

    When ``store_history`` is true the code enters the interactive
    ``In``/``Out`` history and advances ``execution_count`` exactly like a cell
    typed by the user. When ``echo`` is true the code (and its output) are
    mirrored to the real terminal so a watching human sees the interaction.

    Returns a dict describing the outcome.
    """
    result = {
        "stdout": "",
        "stderr": "",
        "result": None,
        "success": True,
        "error": None,
        "execution_count": None,
    }

    count = getattr(shell, "execution_count", None)
    logger.info("execute (In[%s]): %s", count, code.strip())
    if echo:
        prompt = "In [{}] (pytexit.ipython_http): ".format(
            count if count is not None else "?"
        )
        # Indent continuation lines to line up under the prompt, like IPython.
        indent = "\n" + " " * len("In [x] (pytexit.ipython_http): ")
        _echo("\n" + prompt + indent.join(code.rstrip().splitlines()) + "\n")

    stdout = io.StringIO()
    stderr = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout, stderr
    try:
        # silent=False so the run participates in history / display hooks;
        # note IPython forces store_history=False when silent=True.
        exec_result = shell.run_cell(code, store_history=store_history, silent=False)
        if exec_result.error_before_exec is not None:
            result["success"] = False
            result["error"] = repr(exec_result.error_before_exec)
        elif exec_result.error_in_exec is not None:
            result["success"] = False
            result["error"] = repr(exec_result.error_in_exec)
        if exec_result.result is not None:
            try:
                result["result"] = repr(exec_result.result)
            except Exception:  # pragma: no cover - defensive
                result["result"] = "<unrepresentable result>"
    except Exception:  # pragma: no cover - defensive
        result["success"] = False
        result["error"] = traceback.format_exc()
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        result["stdout"] = stdout.getvalue()
        result["stderr"] = stderr.getvalue()
        result["execution_count"] = getattr(shell, "execution_count", None)

    if echo:
        if result["stdout"]:
            _echo(result["stdout"])
        if result["stderr"]:
            _echo(result["stderr"])
        if result["result"] is not None:
            _echo("Out[{}]: {}\n".format(result["execution_count"], result["result"]))
        if not result["success"] and result["error"]:
            _echo(result["error"].rstrip() + "\n")

    if not result["success"]:
        logger.warning("execute failed: %s", result["error"])
    return result


def _get_history(shell, raw=True, limit=None):
    """Return the session input history as a list of ``[line_number, code]``."""
    history = []
    hm = getattr(shell, "history_manager", None)
    if hm is None:
        return history
    # get_range over the current session (session=0 means "current session").
    for session, line, source in hm.get_range(session=0, raw=raw):
        history.append([line, source])
    if limit is not None and limit >= 0:
        history = history[-limit:]
    return history


def _user_variables(shell):
    """Return the names of the user-defined variables (no builtins/hidden)."""
    hidden = set(getattr(shell, "user_ns_hidden", {}))
    names = []
    for name, value in shell.user_ns.items():
        if name.startswith("_"):
            continue
        if name in hidden:
            continue
        if name in ("In", "Out", "get_ipython", "exit", "quit", "open"):
            continue
        names.append(name)
    return sorted(names)


def _describe_variable(shell, name):
    """Return a dict describing a single variable, or ``None`` if missing."""
    if name not in shell.user_ns:
        return None
    value = shell.user_ns[name]
    try:
        value_repr = repr(value)
    except Exception:
        value_repr = "<unrepresentable value>"
    return {"name": name, "type": type(value).__name__, "value": value_repr}


def _make_handler(shell, echo=True, store_history=True):
    """Build a request handler class bound to a specific IPython ``shell``."""

    class Handler(BaseHTTPRequestHandler):
        # Route the default request logging through our logger instead of
        # writing straight to stderr (which is noisy inside a notebook).
        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            logger.debug("%s - %s", self.address_string(), format % args)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path == "/":
                self._send_json(
                    {
                        "service": "pytexit ipython_http",
                        "endpoints": {
                            "GET /history": "session input history "
                            "(?raw=0|1&limit=N)",
                            "GET /variables": "names of user variables",
                            "GET /variable/<name>": "repr of a single variable",
                            "POST /execute": "execute code (raw body or "
                            '{"code": "..."})',
                        },
                    }
                )
            elif path == "/history":
                raw = query.get("raw", ["1"])[0] not in ("0", "false", "False")
                limit = query.get("limit", [None])[0]
                try:
                    limit = int(limit) if limit is not None else None
                except ValueError:
                    limit = None
                self._send_json({"history": _get_history(shell, raw=raw, limit=limit)})
            elif path == "/variables":
                self._send_json({"variables": _user_variables(shell)})
            elif path.startswith("/variable/"):
                name = unquote(path[len("/variable/") :])
                described = _describe_variable(shell, name)
                if described is None:
                    self._send_json(
                        {"error": "no such variable: {}".format(name)}, status=404
                    )
                else:
                    self._send_json(described)
            else:
                self._send_json({"error": "not found: {}".format(path)}, status=404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path != "/execute":
                self._send_json({"error": "not found: {}".format(path)}, status=404)
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            body = raw_body.decode("utf-8", errors="replace")

            code = body
            content_type = self.headers.get("Content-Type", "") or ""
            if "application/json" in content_type or body.strip().startswith("{"):
                try:
                    parsed_body = json.loads(body)
                    if isinstance(parsed_body, dict) and "code" in parsed_body:
                        code = parsed_body["code"]
                except ValueError:
                    pass  # fall back to treating the body as raw code

            if not code or not code.strip():
                self._send_json({"error": "no code provided"}, status=400)
                return

            outcome = _capture_execution(
                shell, code, store_history=store_history, echo=echo
            )
            status = 200 if outcome["success"] else 400
            self._send_json(outcome, status=status)

    return Handler


def start_server(shell, host=None, port=None, echo=None, store_history=None):
    """Start the background HTTP server bound to ``shell``.

    Returns the running ``HTTPServer`` instance.
    """
    global _SERVER
    if _SERVER is not None:
        return _SERVER

    if host is None:
        host = os.environ.get("PYTEXIT_HTTP_HOST", DEFAULT_HOST)
    if port is None:
        port = int(os.environ.get("PYTEXIT_HTTP_PORT", DEFAULT_PORT))
    if echo is None:
        echo = _env_flag("PYTEXIT_HTTP_ECHO", True)
    if store_history is None:
        store_history = _env_flag("PYTEXIT_HTTP_STORE_HISTORY", True)

    handler = _make_handler(shell, echo=echo, store_history=store_history)
    server = _ThreadingHTTPServer((host, port), handler)

    thread = threading.Thread(target=server.serve_forever, name="pytexit-http")
    thread.daemon = True
    thread.start()

    server._pytexit_thread = thread  # keep a reference for shutdown
    _SERVER = server
    logger.info("HTTP server started on http://%s:%s", *server.server_address)
    return server


def stop_server():
    """Stop the background HTTP server, if running."""
    global _SERVER
    if _SERVER is None:
        return
    try:
        _SERVER.shutdown()
        _SERVER.server_close()
        logger.info("HTTP server stopped")
    finally:
        _SERVER = None


def _ensure_logging():
    """Give our logger a console handler if the host app configured none."""
    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(name)s] %(levelname)s: %(message)s")
        )
        logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)


def load_ipython_extension(ipython):
    """Entry point used by ``%load_ext pytexit.ipython_http``."""
    _ensure_logging()
    server = start_server(ipython)
    host, port = server.server_address
    print(
        "[pytexit.ipython_http] HTTP server listening on "
        "http://{}:{}  (endpoints: /history, /variables, "
        "/variable/<name>, POST /execute)".format(host, port)
    )


def unload_ipython_extension(ipython):
    """Entry point used by ``%unload_ext pytexit.ipython_http``."""
    stop_server()
    print("[pytexit.ipython_http] HTTP server stopped")
