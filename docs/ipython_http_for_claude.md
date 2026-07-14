# Working with `pytexit.ipython_http` — a manual for Claude

This is a guide for an AI agent (Claude) that has been given the URL of a
running `pytexit.ipython_http` server and needs to inspect and drive a live
IPython/Jupyter session over HTTP.

## What this is

A human has an IPython session open and has loaded the extension:

```
%load_ext pytexit.ipython_http
```

That starts a **dependency-free** HTTP server (Python standard library only),
by default at `http://127.0.0.1:8899`. Through it you can:

- read the session's **input history**,
- **execute code** in the live namespace,
- read the **value of any variable**.

Everything you run is visible to the human: remote code is echoed to their
terminal (`In [N] (pytexit.ipython_http): ...`), recorded in the session's
`In`/`Out` history, and logged. **Assume a person is watching every call you
make.** Keep actions purposeful and explain them.

## Base URL

Confirm the base URL you were given (host/port can be customised via
`PYTEXIT_HTTP_HOST` / `PYTEXIT_HTTP_PORT`). Examples below use
`http://127.0.0.1:8899`. Hit `GET /` first to confirm it's alive and see the
endpoint list.

## Endpoints

| Method & path            | Purpose                                             |
| ------------------------ | --------------------------------------------------- |
| `GET  /`                 | Health check + index of endpoints.                  |
| `GET  /history`          | Session input history.                              |
| `GET  /variables`        | Names of user-defined variables.                    |
| `GET  /variable/<name>`  | `repr`, `type`, and `name` of one variable.         |
| `POST /execute`          | Execute code in the live namespace.                 |

### `GET /history`

Query params:
- `raw` (`0`/`1`, default `1`) — raw vs. transformed input.
- `limit` (int) — return only the N most recent entries.

```bash
curl 'http://127.0.0.1:8899/history?limit=20'
```

Response:
```json
{"history": [[1, "import numpy as np"], [2, "x = np.arange(10)"]]}
```
Each item is `[line_number, source]`.

### `GET /variables`

```bash
curl http://127.0.0.1:8899/variables
```
```json
{"variables": ["df", "model", "x"]}
```
Builtins, `In`/`Out`, `get_ipython`, and underscore/hidden names are filtered
out, so this is the human-defined surface of the namespace.

### `GET /variable/<name>`

```bash
curl http://127.0.0.1:8899/variable/x
```
```json
{"name": "x", "type": "ndarray", "value": "array([0, 1, 2, ...])"}
```
`value` is `repr(...)`, so it is truncated/summarised the same way IPython shows
it. Returns `404` with `{"error": "no such variable: x"}` if it doesn't exist.

### `POST /execute`

Two accepted body formats:

1. Raw source code as the body:
   ```bash
   curl -X POST http://127.0.0.1:8899/execute -d 'y = x.mean()'
   ```
2. JSON `{"code": "..."}` (use this for multi-line code — it avoids shell
   quoting headaches):
   ```bash
   curl -X POST http://127.0.0.1:8899/execute \
     -H 'Content-Type: application/json' \
     -d '{"code": "import numpy as np\nz = np.linalg.norm(x)\nz"}'
   ```

Response shape:
```json
{
  "stdout": "...captured print() output...",
  "stderr": "...captured stderr...",
  "result": "3.1622776601683795",   // repr of the last expression, or null
  "success": true,
  "error": null,                     // repr(exception) or traceback string if failed
  "execution_count": 7               // the In[]/Out[] number this ran as
}
```

- HTTP status is `200` on success, `400` on execution error (and on empty
  code), `404` for unknown paths.
- To read a value back, end the snippet with a bare expression; its `repr`
  comes back in `result`. Or just call `GET /variable/<name>` afterwards.
- Because `store_history` defaults on, each execute advances the session's
  execution count and shows up in `/history` — this is intentional so the human
  can see what you did. If you were told not to touch history, the operator can
  launch with `PYTEXIT_HTTP_STORE_HISTORY=0`.

## Recommended workflow for Claude

1. **Orient before acting.** `GET /` (alive?) → `GET /variables` (what exists?)
   → `GET /history` (what has the human been doing?). Build a mental model
   before running anything.
2. **Inspect, don't mutate, when you only need to look.** Prefer
   `GET /variable/<name>` over an `execute` that prints it. Reads don't change
   state; `execute` does.
3. **Read values without side effects** by executing a bare expression
   (`df.shape`) and reading `result` — this does not rebind anything, though it
   does advance the execution count.
4. **Keep executes small and legible.** One logical step per call. The human is
   watching the echo; a wall of code is hard to follow and hard to undo.
5. **Namespace your scratch variables** (e.g. prefix with `_claude_`) so you
   don't clobber the human's variables. Note these are filtered out of
   `GET /variables` anyway because they start with `_`, so also clean up after
   yourself if you need visible temporaries.
6. **Check `success` every time.** On `false`, read `error` (a `repr` or
   traceback), fix, and retry. Don't proceed as if it worked.
7. **Never assume libraries are imported.** Confirm via `/variables` or a guarded
   `execute` (`"np" in dir()`) before using them.

## Safety and etiquette

- This endpoint runs **arbitrary code with no authentication** inside someone's
  live session. Treat it as you would a shared terminal on their machine.
- **Do not** run destructive operations (deleting files, dropping data,
  overwriting the human's variables, long/blocking calls, network side effects)
  without being explicitly asked. When in doubt, inspect and ask first.
- Anything you print or run is mirrored to the human's terminal and history —
  don't dump huge blobs; summarise. Slicing/`.head()`/`len(...)` beats printing
  a whole dataframe.
- Blocking or long-running code will tie up the request thread and the kernel;
  keep executes quick, or ask the human to run long jobs themselves.

## Quick reference (copy/paste)

```bash
BASE=http://127.0.0.1:8899
curl $BASE/                                   # health + endpoints
curl $BASE/history?limit=10                   # recent input
curl $BASE/variables                          # variable names
curl $BASE/variable/x                         # one variable
curl -X POST $BASE/execute -d 'x.mean()'      # eval an expression
curl -X POST $BASE/execute -H 'Content-Type: application/json' \
     -d '{"code": "y = x + 1\ny"}'            # multi-line via JSON
```
