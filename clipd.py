#!/usr/bin/env python3
"""clipd: clipboard history for GNOME/Wayland, no shell extension.

  clipd.py daemon   record clipboard changes, serve restore/delete/pin/clear requests
  clipd.py gui      history window; launching it again hides it when focused, raises it otherwise

The daemon is an X11 client: Mutter has no data-control protocol but mirrors the
Wayland clipboard to XWayland, where GDK's X11 backend sees changes without focus.
The GUI is Wayland-native. It reads the SQLite file, watches the data directory to
refresh live, and asks the daemon to restore/delete/pin/clear, so the daemon stays the only
writer of history and the only owner of the clipboard. The GUI writes only its own
settings (table `setting`).
"""
import hashlib, os, socket, sqlite3, sys, time, traceback
from pathlib import Path
from urllib.parse import unquote

DB = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "clipd/history.db"
SOCK = Path(os.environ["XDG_RUNTIME_DIR"]) / "clipd.sock"
KEEP, MAX_BYTES = 500, 20 << 20  # history length, per-format size cap
TEXT = ["text/plain;charset=utf-8", "text/plain", "text/html", "text/uri-list", "x-special/gnome-copied-files"]
IMAGE = ["image/png", "image/jpeg", "image/webp", "image/bmp"]  # only the first one offered is kept
SECRET = "x-kde-passwordManagerHint"  # set by KeePassXC, Bitwarden, etc.; such copies are skipped


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(DB)
    db.executescript("""PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS item(id INTEGER PRIMARY KEY, ts REAL, hash TEXT UNIQUE, kind TEXT, preview TEXT);
        CREATE TABLE IF NOT EXISTS fmt(item INTEGER REFERENCES item(id) ON DELETE CASCADE, mime TEXT, data BLOB);
        CREATE TABLE IF NOT EXISTS setting(key TEXT PRIMARY KEY, value TEXT);""")
    if "pinned" not in [c[1] for c in db.execute("PRAGMA table_info(item)")]:  # migrate v1 databases
        db.execute("ALTER TABLE item ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        db.commit()
    return db


def describe(f):
    uris = f.get("text/uri-list", b"").decode(errors="replace").split()
    if uris and all(u.startswith("file://") for u in uris):
        return "files", "  ".join(unquote(u.rsplit("/", 1)[-1]) for u in uris)
    for m in ("text/plain;charset=utf-8", "text/plain"):
        if m in f:
            return "text", f[m].decode(errors="replace").strip()[:300]
    img = next((m for m in IMAGE if m in f), None)
    if img:
        return "image", f"{img}, {len(f[img]) // 1024} KiB"
    return "other", ", ".join(f)


def save(db, f):
    """f: {mime: bytes}. Same content again moves the existing item to the top."""
    if not f:
        return
    h = hashlib.sha256(b"".join(m.encode() + d for m, d in sorted(f.items()))).hexdigest()
    row = db.execute("SELECT id FROM item WHERE hash=?", (h,)).fetchone()
    if row:
        db.execute("UPDATE item SET ts=? WHERE id=?", (time.time(), row[0]))
    else:
        kind, preview = describe(f)
        cur = db.execute("INSERT INTO item(ts,hash,kind,preview) VALUES(?,?,?,?)", (time.time(), h, kind, preview))
        db.executemany("INSERT INTO fmt VALUES(?,?,?)", [(cur.lastrowid, m, d) for m, d in f.items()])
        db.execute("DELETE FROM item WHERE NOT pinned AND id NOT IN "  # pinned items are never pruned
                   "(SELECT id FROM item WHERE NOT pinned ORDER BY ts DESC LIMIT ?)", (KEEP,))
    db.commit()


def daemon():
    os.environ["GDK_BACKEND"] = "x11"  # must precede the Gdk import
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    Gtk.init()
    display = Gdk.Display.get_default()
    if display is None:
        sys.exit("clipd: no X display (is XWayland available and DISPLAY set?)")
    cb, db = display.get_clipboard(), connect()
    FLAGS = Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET

    def capture(cb):
        offered = set(cb.get_formats().get_mime_types() or [])
        if SECRET in offered:
            return
        want = [m for m in TEXT if m in offered] + [m for m in IMAGE if m in offered][:1]
        got = {}

        def step():  # read the wanted formats one at a time, then save
            if not want:
                return save(db, got)
            cb.read_async([want[0]], GLib.PRIORITY_DEFAULT, None, opened)

        def opened(cb, res):
            mime = want.pop(0)
            try:
                stream, _ = cb.read_finish(res)
            except GLib.Error:
                return step()
            out = Gio.MemoryOutputStream.new_resizable()
            out.splice_async(stream, FLAGS, GLib.PRIORITY_DEFAULT, None, done, mime)

        def done(out, res, mime):
            try:
                out.splice_finish(res)
                data = out.steal_as_bytes().get_data()
                if 0 < len(data) <= MAX_BYTES:
                    got[mime] = data
            except GLib.Error:
                pass
            step()

        step()

    def restore(i):
        rows = db.execute("SELECT mime, data FROM fmt WHERE item=?", (i,)).fetchall()
        if rows:
            cb.set_content(Gdk.ContentProvider.new_union(
                [Gdk.ContentProvider.new_for_bytes(m, GLib.Bytes.new(d)) for m, d in rows]))
            db.execute("UPDATE item SET ts=? WHERE id=?", (time.time(), i))
            db.commit()

    def delete(i):
        db.execute("DELETE FROM item WHERE id=?", (i,))
        db.commit()

    def clear():  # everything except pinned items
        db.execute("DELETE FROM item WHERE NOT pinned")
        db.commit()

    def pin(i, on):
        db.execute("UPDATE item SET pinned=? WHERE id=?", (on, i))
        db.commit()

    def serve(fd, _cond):
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(0.5)
            try:
                cmd, _, arg = conn.recv(64).decode().partition(" ")
                {"restore": restore, "delete": delete, "clear": lambda _: clear(),
                 "pin": lambda i: pin(i, 1), "unpin": lambda i: pin(i, 0)}[cmd](int(arg or 0))
                conn.sendall(b"ok")  # no reply = failed or unknown command (e.g. outdated daemon)
            except Exception:
                traceback.print_exc()  # never let a bad request kill the server
        return True  # returning falsy would remove the watch

    SOCK.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(SOCK))
    srv.listen()
    GLib.io_add_watch(srv.fileno(), GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, serve)
    cb.connect("changed", lambda c: None if c.is_local() else capture(c))
    capture(cb)
    GLib.MainLoop().run()


def gui():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango
    db = connect()
    css = Gtk.CssProvider()
    css.load_from_string("row.pinned { background-color: alpha(currentColor, 0.1); }"
                         ".pinbtn { opacity: 0.35; } row.pinned .pinbtn { opacity: 1; }")
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    rows = {}  # item id -> ListBoxRow; kept across refreshes so images are decoded once
    QUERY = ("SELECT id, ts, kind, preview, pinned FROM item "
             "WHERE pinned OR id IN (SELECT id FROM item ORDER BY ts DESC LIMIT 100)")

    def ask(cmd):  # None on success, else a short error for the title bar
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(2)
                s.connect(str(SOCK))
                s.sendall(cmd.encode())
                return None if s.recv(8) == b"ok" else "daemon rejected the command, restart it"
        except OSError:
            return "daemon not running"

    def make_row(i, kind, preview, run):
        content = None
        if kind == "image":
            try:
                data = db.execute("SELECT data FROM fmt WHERE item=? AND mime LIKE 'image/%'", (i,)).fetchone()[0]
                content = Gtk.Picture.new_for_paintable(Gdk.Texture.new_from_bytes(GLib.Bytes.new(data)))
                content.set_properties(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                                       halign=Gtk.Align.START, height_request=80)
            except GLib.Error:
                pass
        if content is None:
            content = Gtk.Label(label=("[%s] " % kind if kind != "text" else "") + preview.replace("\n", " \u23ce "),
                                xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=60)
        content.set_hexpand(True)
        pin = Gtk.ToggleButton(icon_name="view-pin-symbolic", has_frame=False, valign=Gtk.Align.CENTER)
        pin.add_css_class("pinbtn")
        rm = Gtk.Button(icon_name="user-trash-symbolic", has_frame=False, valign=Gtk.Align.CENTER, tooltip_text="Remove")
        pin.connect("clicked", lambda b: run(f"{'pin' if b.get_active() else 'unpin'} {i}"))  # user clicks only
        rm.connect("clicked", lambda b: run(f"delete {i}"))
        box = Gtk.Box(spacing=4, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for w in (content, pin, rm):
            box.append(w)
        row = Gtk.ListBoxRow(child=box)
        row.item_id, row.haystack, row.pin = i, preview.lower(), pin
        return row

    def activate(app):
        win = app.get_active_window()
        if win:  # launched again: hide if focused, otherwise bring to front
            return win.close() if win.is_active() else win.present()
        win = Gtk.ApplicationWindow(application=app, title="clipd", default_width=480, default_height=560)
        entry, lb = Gtk.SearchEntry(placeholder_text="Filter, Enter to copy"), Gtk.ListBox()
        keep = Gtk.CheckButton(label="Stay open", tooltip_text="Keep this window open after copying an item")
        saved = db.execute("SELECT value FROM setting WHERE key='keep_open'").fetchone()
        keep.set_active(saved is not None and saved[0] == "1")
        clear = Gtk.Button(label="Remove all", tooltip_text="Remove every item except pinned ones")
        header = Gtk.HeaderBar()
        header.pack_start(clear)
        header.pack_end(keep)
        win.set_titlebar(header)

        def run(cmd):  # send to daemon, report failure in the title, refresh once it has committed
            err = ask(cmd)
            win.set_title("clipd: " + err if err else "clipd")
            if not err:
                GLib.idle_add(lambda: sync() or False)

        def on_clear(b):  # destructive: needs a second click within 3 s
            if b.get_label() == "Remove all":
                b.set_label("Click again to confirm")
                GLib.timeout_add_seconds(3, lambda: b.set_label("Remove all") or False)
            else:
                b.set_label("Remove all")
                run("clear")

        def select_first():
            n = 0
            while r := lb.get_row_at_index(n):
                if r.get_visible():
                    return lb.select_row(r)
                n += 1
            lb.select_row(None)

        def filter_rows():
            q = entry.get_text().lower()
            for r in rows.values():
                r.set_visible(q in r.haystack)

        def sync():
            seen = set()
            for i, ts, kind, preview, pinned in db.execute(QUERY):
                seen.add(i)
                row = rows.get(i)
                new = row is None
                if new:
                    row = rows[i] = make_row(i, kind, preview, run)
                row.ts, row.pinned = ts, pinned  # sort keys; must be set before append
                row.pin.set_active(bool(pinned))
                (row.add_css_class if pinned else row.remove_css_class)("pinned")
                row.pin.set_tooltip_text("Unpin" if pinned else "Pin")
                if new:
                    lb.append(row)
            for i in set(rows) - seen:
                lb.remove(rows.pop(i))
            lb.invalidate_sort()
            filter_rows()
            if lb.get_selected_row() is None:
                select_first()

        def pick(row):
            run(f"restore {row.item_id}")
            if not keep.get_active():
                win.close()

        def on_key(_c, key, *_):
            if key == Gdk.KEY_Escape:
                win.close()
            elif key == Gdk.KEY_Delete and not isinstance(win.get_focus(), Gtk.Text):
                if row := lb.get_selected_row():
                    run(f"delete {row.item_id}")
                return True
            return False

        def save_keep(b):
            db.execute("INSERT OR REPLACE INTO setting VALUES('keep_open', ?)", (str(int(b.get_active())),))
            db.commit()

        timer = []

        def fire():  # coalesce bursts of file events into one refresh
            timer.clear()
            sync()
            return GLib.SOURCE_REMOVE

        def on_file_event(*_):
            if not timer:
                timer.append(GLib.timeout_add(80, fire))

        lb.set_sort_func(lambda a, b: (b.pinned - a.pinned) or (1 if b.ts > a.ts else -1 if b.ts < a.ts else 0))
        entry.connect("search-changed", lambda e: (filter_rows(), select_first()))
        entry.connect("activate", lambda e: lb.get_selected_row() and pick(lb.get_selected_row()))
        lb.connect("row-activated", lambda _lb, r: pick(r))
        keep.connect("toggled", save_keep)
        clear.connect("clicked", on_clear)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)  # beat the entry's own Escape handling
        keys.connect("key-pressed", on_key)
        win.add_controller(keys)
        monitor = Gio.File.new_for_path(str(DB.parent)).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        monitor.connect("changed", on_file_event)  # the daemon's commits touch the -wal file
        win.connect("close-request", lambda w: monitor.cancel() and False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(entry)
        box.append(Gtk.ScrolledWindow(child=lb, vexpand=True))
        win.set_child(box)
        sync()
        win.present()
        entry.grab_focus()

    app = Gtk.Application(application_id="local.clipd")
    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    {"daemon": daemon, "gui": gui}.get(sys.argv[1] if len(sys.argv) > 1 else "gui", lambda: sys.exit(__doc__))()
