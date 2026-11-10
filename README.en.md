<img src="ghdl/assets/icon.png" width="96" alt="ghdl-dl icon" align="right">

# ghdl-dl

[简体中文](README.md) | **English**

**Fast, resumable downloads for large GitHub release assets.**

`ghdl-dl` splits a file into HTTP `Range` chunks and downloads them in parallel, keeping
per-chunk progress on disk. When GitHub drops the connection — or the signed CDN URL
expires — it reconnects, re-resolves the URL, and continues from the last byte instead of
starting over.

Built for 1 GB+ assets on unstable networks. Pure Python standard library: no runtime
dependencies, no `pip install` required to run from source. CLI + Tkinter GUI.

> The UI is currently Chinese-only; the CLI messages are Chinese too. Code and docs are
> being translated incrementally. PRs welcome.

## Features

- **Parallel range downloads** — configurable connections (default 8), dynamic work queue
  so fast workers pick up new chunks instead of waiting for the slowest one.
- **Resume anywhere** — pause with `Ctrl+C`, close the app, lose power, or kill the process;
  re-run the same command and it continues. Progress is `fsync`ed and atomically saved.
- **Auto-reconnect** — retries forever by default with exponential backoff (1s → 30s).
  Progress resets the backoff, so a flaky link keeps moving forward.
- **Expiring URL refresh** — GitHub release assets redirect to a signed CDN URL that
  expires. On `403/404` the tool re-resolves the original URL and shares the new address
  across workers.
- **Integrity checks** — `--sha256`, or automatically reads the official digest from the
  GitHub API for release assets. Data is only renamed to the final name after verification.
- **Two on-disk modes** — by default chunks are written at offsets into one preallocated
  `.ghdlpart` file (no second full-size merge); `--parts` writes one file per chunk and
  concatenates them at the end.
- **Graceful fallback** — links without `Range` support (e.g. GitHub source archives) are
  downloaded as a single stream.
- **GUI** — task list with progress/speed/ETA, pause/resume/retry, persistent tasks, and
  auto-resume of unfinished downloads on startup.
- **Network-friendly** — HTTP(S) proxy (including CONNECT and proxy auth), `HTTPS_PROXY`
  env vars, GitHub token for private repos, Windows file locking and UTF-8 console support.

## Install

Requires **Python 3.9+**. The GUI needs `tkinter` (bundled with the official Windows and
macOS installers; on Debian/Ubuntu: `sudo apt install python3-tk`).

### From a release (no install)

1. Download the latest release archive and unpack it.
2. Windows: double-click `start-gui.bat` (GUI) or `start-cli.bat` (CLI).
3. Other platforms: `python run_gui.py` / `python run_cli.py --help`.

The launcher scripts look for Python in `PATH`; you can also run the `.py` files directly
with any Python 3.9+.

### pip

```bash
git clone https://github.com/c4h2o/ghdl-dl.git
cd ghdl-dl
pip install .
# or: pipx install .

ghdl-dl --version
ghdl-dl-gui          # GUI (installed as a GUI script, no console window on Windows)
```

The distribution name is `ghdl-dl` to avoid clashing with [GHDL](https://github.com/ghdl/ghdl),
the VHDL simulator. The import package is still `ghdl`, and `python -m ghdl` works.

## Quick start

```bash
# save into the current directory, filename inferred from the server
ghdl-dl https://github.com/owner/repo/releases/download/v1.0/big.zip

# 12 connections, explicit output
ghdl-dl <URL> -o D:/Downloads -n 12

# verify, private repo, proxy
ghdl-dl <URL> --sha256 <64-hex>
ghdl-dl <URL> --token ghp_xxx
ghdl-dl <URL> --proxy http://127.0.0.1:7890

# inspect size / Range support without downloading
ghdl-dl <URL> --info

# one file per chunk, concatenated at the end
ghdl-dl <URL> --parts
```

Interrupting with `Ctrl+C` pauses and saves progress (exit code `3`). Running the same
command again resumes. Press `Ctrl+C` twice to force-quit.

## How it works

1. **Probe** with `Range: bytes=0-0`: learn the total size, whether the server supports
   ranges (`206`), the real filename, and the final signed CDN URL.
2. **Plan** chunks (about four per connection, clamped to a sensible size) and enqueue them.
3. **Download** each chunk with `Range: bytes=start-end`; workers write directly at their
   file offset.
4. **Persist** `[start, end, done]` per chunk every 5 seconds: fsync data, atomically replace
   the JSON state. On restart the layout is validated (URL, size, chunk map) before resuming.
5. **Verify and finish**: check the final size, compare SHA-256 when available, then
   `os.replace` to the final filename. Failed verification keeps the data for inspection.

The signed CDN URL is re-resolved on expiry; the engine treats `403/404` as "refresh and
retry" rather than a fatal error.

## FAQ

**Why can't GitHub source archives (`/archive/...zip`) be parallelised?**
They are generated on the fly and don't support `Range`, so the tool falls back to a single
stream and warns that a dropped connection means starting over. Release assets do support
`Range`.

**Private repositories?**
Create a token with `repo` scope and pass `--token` (or set `GITHUB_TOKEN`). The token is
only sent to GitHub hosts, never to the signed CDN.

**How many connections?**
8 is a good default. GitHub rate-limits per IP; 8–16 usually saturates a home connection
without triggering `429`.

**TLS `CERTIFICATE_VERIFY_FAILED`?**
Usually a corporate/campus proxy intercepting HTTPS. Install the proxy's root certificate,
configure the proxy correctly, or use `--insecure` temporarily (not recommended).

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (including "already exists, skipped") |
| 1 | Failure |
| 2 | Invalid arguments |
| 3 | Paused — safe to resume |

## License

[MIT](LICENSE). Use it, modify it, ship it commercially; just keep the copyright notice
and license text.

## Contributing & security

- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md) — please report vulnerabilities privately.
- [Release process](docs/RELEASING.md)
