"""Live view: a local web page that redraws the schematic/PCB on every save.

KiCad's schematic editor can't be driven live from outside (no eeschema IPC
commands yet), so the agent edits files and this page shows the result within
about a second: kicad-cli renders exact SVGs, changed parts are circled, and a
sidebar shows the netlist diff, ERC counts, checkpoints and review findings.

Standalone:  python -m copilot.viewer <project-folder> [--port 8765]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import queue
import re
import shutil
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import checks, kicad
from .checkpoints import CheckpointStore
from .kicad import Project, resolve_project
from .netlist import Netlist, diff

STATIC = Path(__file__).resolve().parent / "static"
KEEP_VERSIONS = 4
MAX_EVENTS = 40

_SYMBOL_BLOCK = re.compile(r"\n\t\(symbol\n(.*?)\n\t\)", re.S)
_AT = re.compile(r"\(at ([-\d.]+) ([-\d.]+)")
_REFERENCE = re.compile(r'\(property "Reference" "([^"]+)"')
_SHEET_BLOCK = re.compile(r"\n\t\(sheet\n(.*?)\n\t\)", re.S)


def symbol_positions(sch: Path) -> dict[str, list[float]]:
    """Reference -> [x, y] in mm for every placed symbol in one schematic file."""
    if not sch.exists():
        return {}
    text = sch.read_text(encoding="utf-8", errors="replace")
    out = {}
    for block in _SYMBOL_BLOCK.findall(text):
        at, ref = _AT.search(block), _REFERENCE.search(block)
        if at and ref and not ref.group(1).startswith("#"):
            out[ref.group(1)] = [float(at.group(1)), float(at.group(2))]
    return out


def sheet_files(sch: Path) -> dict[str, str]:
    """Sheet name -> sheet file for the sub-sheets placed in a schematic."""
    text = sch.read_text(encoding="utf-8", errors="replace") if sch.exists() else ""
    out = {}
    for block in _SHEET_BLOCK.findall(text):
        name = re.search(r'\(property "Sheetname" "([^"]*)"', block)
        file = re.search(r'\(property "Sheetfile" "([^"]*)"', block)
        if name and file:
            out[name.group(1)] = file.group(1)
    return out


class LiveView:
    def __init__(self, project: Project):
        self.project = project
        self.version = 0
        self.pages: list[dict] = []
        self.pcb: dict | None = None
        self.events: list[dict] = []
        self.review: dict | None = None
        self.prev_netlist: Netlist | None = None
        self.prev_pcb_mtime = 0.0
        self.error: str | None = None
        self.pending: dict | None = None
        self.lock = threading.RLock()
        self.subscribers: list[queue.Queue] = []
        self._stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

    # ------------------------------------------------------------ rendering
    @property
    def view_dir(self) -> Path:
        return self.project.state_dir / "view"

    def refresh(self, reason: str = "change", files: list[str] | None = None) -> None:
        with self.lock:
            event: dict = {"time": datetime.now().strftime("%H:%M:%S"), "reason": reason, "files": files or []}
            # The engine writes a file in several steps; a render can catch it half-written.
            for attempt in range(3):
                self.version += 1
                try:
                    self._render(event)
                    self.error = None
                    break
                except Exception as exc:
                    self.error = f"{type(exc).__name__}: {exc}"
                    time.sleep(0.6)
            else:
                event["error"] = self.error
            event["version"] = self.version
            if event.get("error") or reason == "start" or event.get("pcb_changed") or event.get("diff") is not None:
                self.events.insert(0, event)
                del self.events[MAX_EVENTS:]
            self._prune()
        self.notify()

    def _snapshot(self, vdir: Path) -> Project:
        """Copy the design files and render from the copy, so kicad-cli never holds the
        user's files open while the engine is writing them (Windows file-sharing races)."""
        src_dir = vdir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        for f in self.project.tracked_files():
            rel = f.relative_to(self.project.dir)
            data = f.read_bytes()
            if f.suffix in (".kicad_sch", ".kicad_pcb") and not data.rstrip().endswith(b")"):
                raise RuntimeError(f"{rel} is mid-write")
            (src_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (src_dir / rel).write_bytes(data)
        return Project(src_dir, self.project.name)

    def _render(self, event: dict) -> None:
        live = self.project
        vdir = self.view_dir / f"v{self.version}"
        p = self._snapshot(vdir)
        svgs = kicad.export_svgs(p.sch, vdir / "sch")
        names = sheet_files(p.sch)
        pages = []
        for svg in svgs:
            sheet_name = svg.stem[len(p.name) + 1:] if svg.stem != p.name else ""
            src = p.sch if not sheet_name else p.dir / names.get(sheet_name, "")
            expand_svg_to_content(svg, src)
            pages.append({
                "name": sheet_name or "Root",
                "url": f"/view/v{self.version}/sch/{svg.name}",
                "size": _svg_size(svg),
                "positions": symbol_positions(src) if src.is_file() else {},
            })
        pages.sort(key=lambda pg: pg["name"] != "Root")

        nl = Netlist.from_schematic(p.sch, vdir / "net")
        erc = checks.run_erc(p.sch, vdir / "erc")

        pcb_changed = live.pcb.exists() and (live.pcb.stat().st_mtime != self.prev_pcb_mtime or self.pcb is None)
        if pcb_changed:
            pcb_svgs = kicad.export_svgs(p.pcb, vdir / "pcb")
            self.pcb = {"url": f"/view/v{self.version}/pcb/{pcb_svgs[0].name}", "size": _svg_size(pcb_svgs[0])}
            self.prev_pcb_mtime = live.pcb.stat().st_mtime
            if event["reason"] != "start":
                event["pcb_changed"] = True
        # Otherwise self.pcb keeps pointing at the last render; _prune keeps that folder.

        self.pages = pages
        if self.prev_netlist is not None:
            d = diff(self.prev_netlist, nl)
            event["diff"] = d.text() if not d.empty else "Layout only (no connectivity or part changes)."
            event["changed_refs"] = sorted(d.touched_refs())
        self.prev_netlist = nl
        event["erc"] = {
            "errors": sum(v.severity == "error" for v in erc),
            "warnings": sum(v.severity == "warning" for v in erc),
        }
        self.pending = self._since_checkpoint(nl)

    def _since_checkpoint(self, nl: Netlist) -> dict | None:
        """Everything that changed since the newest checkpoint (what the agent is about to verify)."""
        try:
            store = CheckpointStore(self.project)
            cps = store.list(limit=1)
            if not cps:
                return None
            d = diff(store.netlist(cps[0].id), nl)
            return {"checkpoint": cps[0].to_dict(), "diff": d.text(), "changed_refs": sorted(d.touched_refs())}
        except Exception as exc:
            return {"error": str(exc)}

    def _prune(self) -> None:
        keep = {f"v{v}" for v in range(self.version - KEEP_VERSIONS + 1, self.version + 1)}
        if self.pcb:
            keep.add(self.pcb["url"].split("/")[2])
        for d in self.view_dir.glob("v*"):
            if d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)

    # ------------------------------------------------------------ state + push
    def state(self) -> dict:
        with self.lock:
            try:
                cps = [c.to_dict() for c in CheckpointStore(self.project).list(limit=12, include_auto=False)]
            except Exception:
                cps = []
            return {
                "project": self.project.name,
                "dir": str(self.project.dir),
                "version": self.version,
                "pages": self.pages,
                "pcb": self.pcb,
                "events": self.events,
                "checkpoints": cps,
                "review": self.review,
                "pending": self.pending,
                "error": self.error,
            }

    def notify(self) -> None:
        for q in list(self.subscribers):
            q.put(self.version)

    # ------------------------------------------------------------ watching
    def start_watching(self) -> None:
        from watchfiles import watch

        def is_design_file(_change, path: str) -> bool:
            return path.endswith((".kicad_sch", ".kicad_pcb")) and ".copilot" not in path and "-backups" not in path

        def loop():
            for changes in watch(self.project.dir, watch_filter=is_design_file, debounce=700,
                                 stop_event=self._stop, recursive=True):
                files = sorted({Path(p).name for _, p in changes})
                self.refresh("change", files)

        self._watch_thread = threading.Thread(target=loop, name="copilot-watch", daemon=True)
        self._watch_thread.start()

    def stop(self) -> None:
        self._stop.set()


def _svg_size(svg: Path) -> list[float]:
    """[x, y, width, height] of the SVG's viewBox."""
    head = svg.read_text(encoding="utf-8", errors="replace")[:2000]
    m = re.search(r'viewBox="([-\d.]+) ([-\d.]+) ([\d.]+) ([\d.]+)"', head)
    return [float(g) for g in m.groups()] if m else [0.0, 0.0, 297.0, 210.0]


_COORD = re.compile(r"\((?:at|xy|start|end) ([-\d.]+) ([-\d.]+)")


def content_bounds(sch: Path, margin: float = 12.0) -> tuple[float, float, float, float] | None:
    """Bounding box (mm) of everything placed in a schematic, ignoring the symbol library section."""
    if not sch.is_file():
        return None
    text = sch.read_text(encoding="utf-8", errors="replace")
    start = text.find("\n\t(lib_symbols")
    if start != -1:
        end = text.find("\n\t)", start)
        text = text[:start] + text[end:]
    pts = [(float(x), float(y)) for x, y in _COORD.findall(text)]
    if not pts:
        return None
    xs, ys = [x for x, _ in pts], [y for _, y in pts]
    return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin


def expand_svg_to_content(svg: Path, sch: Path) -> None:
    """kicad-cli clips the SVG to the paper; parts placed off-page are still in the file.
    Widen the viewBox to cover the page plus everything placed outside it."""
    bounds = content_bounds(sch)
    if not bounds:
        return
    text = svg.read_text(encoding="utf-8", errors="replace")
    x, y, w, h = _svg_size(svg)
    x0, y0 = min(x, bounds[0]), min(y, bounds[1])
    x1, y1 = max(x + w, bounds[2]), max(y + h, bounds[3])
    if (x0, y0, x1, y1) == (x, y, x + w, y + h):
        return
    head, rest = text[:2000], text[2000:]
    head = re.sub(r'viewBox="[^"]*"', f'viewBox="{x0:.4f} {y0:.4f} {x1 - x0:.4f} {y1 - y0:.4f}"', head, count=1)
    head = re.sub(r'width="[\d.]+mm" height="[\d.]+mm"', f'width="{x1 - x0:.4f}mm" height="{y1 - y0:.4f}mm"', head, count=1)
    svg.write_text(head + rest, encoding="utf-8")


# =================================================================== HTTP
class _Handler(BaseHTTPRequestHandler):
    view: LiveView  # set on the class by serve()

    def log_message(self, *_args):  # keep MCP stdio and terminals quiet
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        view = type(self).view
        if path in ("/", "/index.html"):
            return self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(json.dumps(view.state()).encode(), "application/json")
        if path == "/events":
            return self._stream(view)
        if path.startswith("/static/"):
            return self._file(STATIC, path[len("/static/"):], cache=True)
        if path.startswith("/view/"):
            return self._file(view.view_dir, path[len("/view/"):], cache=True)
        if path.startswith("/files/"):  # raw KiCad files for the interactive (KiCanvas) tab
            return self._file(view.project.dir, path[len("/files/"):])
        self._send(b"not found", "text/plain", 404)

    def _file(self, root: Path, rel: str, cache: bool = False):
        target = (root / rel).resolve()
        if root.resolve() not in target.parents or not target.is_file():
            return self._send(b"not found", "text/plain", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix in (".kicad_sch", ".kicad_pcb", ".kicad_pro"):
            ctype = "text/plain; charset=utf-8"
        self._send(target.read_bytes(), ctype, cache=cache)

    def _stream(self, view: LiveView):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        view.subscribers.append(q)
        try:
            self.wfile.write(f"data: {view.version}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    v = q.get(timeout=15)
                    self.wfile.write(f"data: {v}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            if q in view.subscribers:
                view.subscribers.remove(q)


_running: dict = {"server": None, "view": None, "url": None}


def ensure_viewer(project: Project, port: int = 8765) -> str:
    """Start the live view (or point the running one at another project). Returns the URL."""
    if _running["server"] is not None:
        view: LiveView = _running["view"]
        if view.project.dir != project.dir:
            view.stop()
            new = LiveView(project)
            new.subscribers = view.subscribers
            new.version = view.version
            _Handler.view = new
            _running["view"] = new
            new.refresh("start")
            new.start_watching()
        return _running["url"]
    view = LiveView(project)
    _Handler.view = view
    httpd = None
    for candidate in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), _Handler)
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"No free port in {port}-{port + 19}")
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    threading.Thread(target=httpd.serve_forever, name="copilot-http", daemon=True).start()
    _running.update(server=httpd, view=view, url=url)
    view.refresh("start")
    view.start_watching()
    return url


def current_url() -> str | None:
    return _running["url"]


def publish_review(project: Project, review: dict) -> None:
    view: LiveView | None = _running["view"]
    if view and view.project.dir == project.dir:
        with view.lock:
            view.review = review
        view.notify()


def main() -> None:
    ap = argparse.ArgumentParser(description="Live view of a KiCad project")
    ap.add_argument("project")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    url = ensure_viewer(resolve_project(args.project), args.port)
    print(f"Live view: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
