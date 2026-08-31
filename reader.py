#!/usr/bin/env python3
"""Textual reader, the replacement for newsboat. Design notes: design/reader.md.

It keeps reading newsboat's cache.db with an identical schema, so read state
carries over with no migration. Three layers live here: storage (sqlite),
fetching (feedparser), and UI (Textual).

Invariants, documented in design/reader.md. Change the doc before the code:
- No DELETE. Feeds that rotate out or get unsubscribed keep their rows.
- unread changes in exactly two places: 1 on insert, 0 when read.
"""
import os
import sys
from pathlib import Path

# Re-exec into the repo's venv, before feedparser and textual: on the system
# python those imports fail outright.
VENV = Path(__file__).resolve().parent / ".venv"
if Path(sys.prefix) != VENV and (VENV / "bin" / "python3").exists():
    _py = str(VENV / "bin" / "python3")
    os.execv(_py, [_py, str(Path(__file__).resolve()), *sys.argv[1:]])

import calendar
import re
import shlex
import sqlite3
import threading
import time
from typing import NamedTuple

import feedparser

import grab

DB = grab.STATE / "cache.db"

# Copied from newsboat's cache.db .schema (version 2.33). Creates the database on
# a fresh install; on an existing one every IF NOT EXISTS is a no-op. Columns we
# never touch (enclosure/flags/base) are carried along, because an identical
# schema is what makes going back possible.
DDL = """
CREATE TABLE IF NOT EXISTS rss_feed (
  rssurl VARCHAR(1024) PRIMARY KEY NOT NULL,
  url VARCHAR(1024) NOT NULL,
  title VARCHAR(1024) NOT NULL,
  lastmodified INTEGER(11) NOT NULL DEFAULT 0,
  is_rtl INTEGER(1) NOT NULL DEFAULT 0,
  etag VARCHAR(128) NOT NULL DEFAULT "");
CREATE TABLE IF NOT EXISTS rss_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
  guid VARCHAR(64) NOT NULL,
  title VARCHAR(1024) NOT NULL,
  author VARCHAR(1024) NOT NULL,
  url VARCHAR(1024) NOT NULL,
  feedurl VARCHAR(1024) NOT NULL,
  pubDate INTEGER NOT NULL,
  content VARCHAR(65535) NOT NULL,
  unread INTEGER(1) NOT NULL,
  enclosure_url VARCHAR(1024),
  enclosure_type VARCHAR(1024),
  enqueued INTEGER(1) NOT NULL DEFAULT 0,
  flags VARCHAR(52),
  deleted INTEGER(1) NOT NULL DEFAULT 0,
  base VARCHAR(128) NOT NULL DEFAULT "",
  content_mime_type VARCHAR(255) NOT NULL DEFAULT "",
  enclosure_description VARCHAR(1024) NOT NULL DEFAULT "",
  enclosure_description_mime_type VARCHAR(128) NOT NULL DEFAULT "");
CREATE INDEX IF NOT EXISTS idx_guid ON rss_item(guid);
CREATE INDEX IF NOT EXISTS idx_feedurl ON rss_item(feedurl);
CREATE INDEX IF NOT EXISTS idx_deleted ON rss_item(deleted);
CREATE INDEX IF NOT EXISTS idx_rssurl ON rss_feed(rssurl);
CREATE INDEX IF NOT EXISTS idx_lastmodified ON rss_feed(lastmodified);
"""


# -- storage -----------------------------------------------------------

def connect(path: Path | str = None) -> sqlite3.Connection:
    """Open or create the database. timeout=5 is busy_timeout: wait out another
    writer rather than raise. This process holds two connections, since a sqlite
    connection cannot cross threads: one for the UI, one for the refresh thread.
    Export runs in other processes but never touches unread."""
    if path is None:
        DB.parent.mkdir(parents=True, exist_ok=True)
        path = DB
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    # In delete mode a read blocks a write, and the UI connection reading while the
    # refresh thread writes locks them both out until one raises (measured: database
    # is locked after 5.39s). WAL removes that; writers are still serialised, but
    # these writes take milliseconds and busy_timeout covers it. Logging mode only,
    # so old sqlite tools still read the file.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    return conn


def feeds_with_unread(conn) -> dict[str, int]:
    """feedurl -> unread count."""
    return {r["feedurl"]: r["n"] for r in conn.execute(
        "SELECT feedurl, sum(unread) AS n FROM rss_item WHERE deleted=0 GROUP BY feedurl")}


def items_of(conn, feedurl: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, guid, title, author, url, unread, pubDate, content FROM rss_item "
        "WHERE feedurl=? AND deleted=0 ORDER BY pubDate DESC, id DESC", (feedurl,)).fetchall()


def mark_read(conn, item_id: int) -> None:
    try:
        conn.execute("UPDATE rss_item SET unread=0 WHERE id=?", (item_id,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


# -- fetching ----------------------------------------------------------

class Feed(NamedTuple):
    """One parsed feed line. The config string is interpreted only here, so
    fetching, rotation, deduplication, and name fallback all work off structured
    fields and nobody downstream can re-split the raw string."""

    url: str
    alias: str | None = None          # the "~display name" from the config
    tags: tuple[str, ...] = ()        # trailing bare words, used by rotate

    def name(self, saved_title: str | None = None) -> str:
        """Config alias, then the feed's own title, then the URL."""
        return self.alias or saved_title or self.url


def parse_feed_line(line: str) -> Feed:
    """One config.toml feeds line to a Feed. A subset of newsboat's urls syntax:
    'https://x/feed "~display name" tag1 tag2'."""
    try:
        tokens = shlex.split(line)
    except ValueError as e:                      # unbalanced quotes: say which line
        sys.exit(f"Cannot parse this feeds line in config.toml ({e}): {line}")
    url, alias, tags = "", None, []
    for i, tok in enumerate(tokens):
        if i == 0:                               # first token is the url, even if empty
            url = tok
        elif tok.startswith("~"):
            alias = tok[1:]
        else:
            tags.append(tok)
    return Feed(url, alias, tuple(tags))


def todays_feeds(cfg: dict, day: int) -> list[Feed]:
    """Config to the feeds for today: parse once, dedupe by url keeping the first,
    then rotate by tag. Both the refresh loop and the UI consume this result.

    Rotation only looks at parsed tags. Splitting the raw line on spaces would read
    a bare word inside `"~The ai digest" eng` as a tag and hide that feed on ai
    days. Lines without a url are dropped."""
    seen: dict[str, Feed] = {}
    for line in cfg.get("feeds") or []:
        if (f := parse_feed_line(line)).url:
            seen.setdefault(f.url, f)            # dedupe first, so "first wins" is date-independent
    feeds = list(seen.values())
    if rotate := cfg.get("rotate"):
        hidden = set(rotate) - {rotate[day % len(rotate)]}
        feeds = [f for f in feeds if not (set(f.tags) & hidden)]   # untagged feeds always show
    return feeds


WDAY = "Mon Tue Wed Thu Fri Sat Sun".split()
MON = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
RFC822ISH = re.compile(r"[A-Z][a-z]{2}, \d{1,2} ")


def rfc822(t: time.struct_time) -> str:
    """newsboat's w3cdtf_to_rfc822 output: fixed English, fixed +0000, no locale."""
    return (f"{WDAY[t.tm_wday]}, {t.tm_mday:02d} {MON[t.tm_mon - 1]} {t.tm_year} "
            f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} +0000")


def entry_guid(e: dict) -> str:
    """newsboat's four-level fallback (rssparser.cpp get_guid): guid, link+pubDate,
    link, title. Every guid in the existing database was computed this way, so
    changing this brings the whole database back as unread.

    The pubDate level has to match byte for byte. RSS2 <pubDate> is concatenated
    raw, which is what HN's old guids contain. Atom <published>/<updated> and
    dc:date are W3CDTF and get converted to RFC822 first, so "does it look like
    RFC822" decides which path to take. Two rare cases still differ and are
    accepted: RSS2 feeds carrying a non-RFC822 date, and <id> with an xml:base."""
    if g := e.get("id"):
        return g
    link, pub = e.get("link") or "", e.get("published") or ""
    if not link:
        return e.get("title") or ""
    if pub:
        if not RFC822ISH.match(pub) and (t := e.get("published_parsed")):
            pub = rfc822(t)
    elif t := e.get("updated_parsed"):       # Atom with only <updated>: newsboat uses it
        pub = rfc822(t)
    return link + pub


def entry_date(e: dict) -> int:
    for key in ("published_parsed", "updated_parsed"):
        if t := e.get(key):
            # clamp to [1970, 2100): a feed dated 1969 or 10000 makes strftime blow up the list
            return max(0, min(calendar.timegm(t), 4102444799))
    return int(time.time())


def entry_content(e: dict) -> str:
    if cs := e.get("content"):
        return cs[0].get("value") or ""
    return e.get("summary") or ""


def fetch(url: str, etag: str = "", lastmodified: int = 0):
    """Fetch one feed, returning the feedparser result plus status/etag/lastmodified.

    We fetch the bytes and hand feedparser only bytes. Letting it fetch the URL
    makes it resolve relative guids against the feed address (resolve_relative_uris
    does not cover that path), while newsboat stored the raw string, so every guid
    in the old database stops matching. urllib3 also decodes gzip/br/zstd as a
    stream, same reason as grab.extract."""
    import urllib3

    headers = {"User-Agent": grab.UA}
    if etag:
        headers["If-None-Match"] = etag
    if lastmodified:
        headers["If-Modified-Since"] = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(lastmodified))
    # 15s: a thread worker cannot be cancelled, so quitting waits out a whole round.
    # Retry(3, connect=0, read=0) keeps redirects but not the default connect/read
    # retries, which turn that 15s straggler into 60s (measured on a dead feed).
    resp = urllib3.request("GET", url, timeout=15.0, headers=headers,
                           retries=urllib3.Retry(3, connect=0, read=0))
    if resp.status >= 400:
        raise RuntimeError(f"HTTP {resp.status}")
    parsed = feedparser.parse(resp.data if resp.status == 200 else b"")
    parsed.status = resp.status
    parsed["etag"] = resp.headers.get("ETag") or ""
    if lm := resp.headers.get("Last-Modified"):
        from email.utils import parsedate_to_datetime
        try:
            parsed["lastmodified"] = int(parsedate_to_datetime(lm).timestamp())
        except ValueError:
            parsed["lastmodified"] = 0
    return parsed


def store_feed(conn, url: str, title: str | None, parsed) -> int:
    """Write one feed's parse result, returning the number of new items."""
    if getattr(parsed, "status", None) == 304 or not parsed.entries:
        return 0                                 # 304, failed fetch, or empty feed: no writes
    conn.execute("INSERT OR IGNORE INTO rss_feed (rssurl, url, title) VALUES (?, ?, ?)",
                 (url, parsed.feed.get("link") or url,
                  title or parsed.feed.get("title") or url))
    conn.execute(
        "UPDATE rss_feed SET etag=?, lastmodified=? WHERE rssurl=?",
        (parsed.get("etag") or "", parsed.get("lastmodified") or 0, url))
    fresh = 0
    for e in parsed.entries:
        guid = entry_guid(e)
        if not guid or conn.execute(
                "SELECT 1 FROM rss_item WHERE guid=?", (guid,)).fetchone():
            continue                             # empty guid, or already seen
        conn.execute(
            "INSERT INTO rss_item (guid, title, author, url, feedurl, pubDate, content, unread)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (guid, e.get("title") or "(untitled)", e.get("author") or "",
             e.get("link") or "", url, entry_date(e), entry_content(e)))
        fresh += 1
    conn.commit()
    return fresh


def refresh(conn, feeds: list[Feed], report=lambda url, got: None) -> int:
    """Fetch every feed concurrently, write on this thread: sqlite gets one writer.
    A failing feed is reported and skipped. Returns the total new item count."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    heads = {f.url: (r["etag"], r["lastmodified"]) if (r := conn.execute(
        "SELECT etag, lastmodified FROM rss_feed WHERE rssurl=?", (f.url,)).fetchone())
        else ("", 0) for f in feeds}
    total = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fetch, f.url, *heads[f.url]): f for f in feeds}
        for job in as_completed(jobs):
            url, title = jobs[job].url, jobs[job].alias
            try:
                got = store_feed(conn, url, title, job.result())
                total += got
                report(url, got)
            except Exception as e:               # one feed must not end the round
                conn.rollback()                  # drop the half-finished transaction
                report(url, e)
    return total


# -- UI ----------------------------------------------------------------
# No point deferring the Textual imports: this file is the reader.

from rich.text import Text                       # noqa: E402 (textual ships rich)
from textual import work                         # noqa: E402
from textual.app import App, ComposeResult       # noqa: E402
from textual.binding import Binding              # noqa: E402
from textual.containers import VerticalScroll    # noqa: E402
from textual.screen import Screen                # noqa: E402
from textual.worker import WorkerError           # noqa: E402
from textual.widgets import DataTable, Footer, Input, Markdown  # noqa: E402

RELOAD_MINUTES = 60                  # newsboat's reload-time; nobody ever changed it

# Cyberpunk palette: rose for emphasis, cyan for anything pressable, pink used once
# for the header. Hierarchy comes from brightness, applied per row rather than in CSS.
CSS = """
Screen { background: #121212; }
DataTable {
    height: 1fr;
    margin: 1 2 0 2;
    background: #121212;
    overflow-x: hidden;
}
DataTable > .datatable--header { color: #ff5faf; background: #1c1c1c; text-style: bold; }
DataTable > .datatable--odd-row { background: #161616; }
DataTable > .datatable--cursor { background: #303030; }
VerticalScroll { margin: 1 3 0 3; padding: 0 1; }
Screen * {
    scrollbar-size-vertical: 1;
    scrollbar-color: #303030;
    scrollbar-color-hover: #585858;
    scrollbar-color-active: #5fd7d7;
    scrollbar-background: #121212;
    scrollbar-background-hover: #121212;
    scrollbar-background-active: #121212;
}
Footer { background: #1c1c1c; }
Footer .footer--key { color: #5fd7d7; }
#filter { background: #1c1c1c; color: #d0d0d0; border: none; height: 1; margin: 0 2; padding: 0 1; }
"""

UNREAD, READ, MARK = "bold #d0d0d0", "#585858", "#5fd7d7 bold"


def summary_md(row) -> str:
    """The feed's own summary (the content column, HTML) as readable markdown.
    Same treatment as HN comments: blank lines for <p>, strip the rest."""
    body = grab.hn_comment_md(row["content"]) if row["content"] else "(this feed ships no summary — press o for the full text)"
    return grab.document(row["title"], row["url"], body)


class ReadingSession:
    """One reading: the current article, its mode, and one producer's lifetime.

    Read-marking, mode switching, and stop semantics all live here, so the reading
    screen only displays and navigates. Stopping is cooperative, since Python
    threads cannot be killed: switching or leaving sets the event, the old producer
    stops at its next fragment, and its output dies with that event rather than
    mixing into the new mode."""

    def __init__(self, conn, row, mode: str = "summary"):
        self.conn, self.row, self.mode = conn, dict(row), mode
        self.stop = threading.Event()

    def begin(self) -> Exception | None:
        """Opening the article marks it read. A write failure comes back as an
        exception: the UI shows the error and leaves the row unread, but the article
        still opens."""
        try:
            mark_read(self.conn, self.row["id"])
        except sqlite3.Error as e:
            return e
        self.row["unread"] = 0
        return None

    def switch(self, mode: str) -> None:
        self.stop.set()                          # old producer stops
        self.stop, self.mode = threading.Event(), mode

    def end(self) -> None:
        self.stop.set()

    def producer(self):
        """(fragment generator, its stop). Bound together so the background thread
        only ever sees the mode it was handed, and cannot read a newer mode after
        extract returns and translate on behalf of a session that is already dead.
        The generator runs on the producer thread, so it touches no UI."""
        mode, stop, r = self.mode, self.stop, self.row

        def pieces():
            if mode == "summary":                # already in the database
                yield summary_md(r)
                return
            got = grab.extract(r["url"])
            if stop.is_set():                    # user left or switched during the fetch
                return
            if not got:
                raise RuntimeError(f"No article text found (anti-bot?): {r['url']}")
            doc = grab.document(r["title"] or got[0], r["url"], got[1])
            if mode == "original":
                yield doc
            else:
                yield from grab.punct_lines(grab.translate_stream(doc, stop))

        return pieces, stop


class StreamScreen(Screen):
    """Reading screen: renders what the session produces, o / t switch modes in
    place. Streaming and stopping live in grab.stream_md, shared with --read --zh."""

    BINDINGS = [
        ("q", "back", "Back"),
        ("o", "original", "Full text"),
        ("t", "chinese", "Chinese"),
        Binding("left", "back", priority=True, show=False),
    ]

    def __init__(self, session: ReadingSession):
        super().__init__()
        self.session = session
        self.stream_worker = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Markdown()
        yield Footer()

    def on_mount(self) -> None:
        # No anchor(): reading starts at the top, the stream fills in below.
        self.stream_worker = self.stream_it()

    def on_unmount(self) -> None:
        self.session.end()       # leaving stops the fetch and the translation

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_original(self) -> None:
        self.switch_mode("original")

    def action_chinese(self) -> None:
        self.switch_mode("chinese")

    @work(exclusive=True, group="switch", exit_on_error=False)   # o t o: only the last one counts
    async def switch_mode(self, mode: str) -> None:
        """Let the old producer finish stopping before clearing the screen, or its
        last fragments land on top of the new content."""
        if self.session.mode == mode:
            return
        self.session.switch(mode)
        if self.stream_worker is not None:
            try:
                await self.stream_worker.wait()
            except WorkerError:                  # how the old worker ended does not matter
                pass
        if not self.is_attached:
            return    # the user pressed left while we waited; query_one would raise NoMatches
        self.stream_worker = self.stream_it()

    @work(group="stream", exit_on_error=False)   # a broken reading screen must not kill the app
    async def stream_it(self) -> None:
        try:
            md = self.query_one(Markdown)        # keep this inside the try: it raises
                                                 # NoMatches once the screen is detached, and
                                                 # the notify below is what the is_attached
                                                 # guard in switch_mode is tested against
            await md.update("")                  # clear the previous mode
            produce, stop = self.session.producer()
            await grab.stream_md(md, produce, stop)
        except Exception as e:                   # stream_md shows producer errors itself; this
            self.notify(f"Render error: {e}", severity="error")   # catches the consumer side,
                                                 # where swallowing means a blank page forever


class ArticlesScreen(Screen):
    """One feed's articles. Space multi-selects and the selection accumulates across
    feeds (it lives on the app), b exports the batch."""

    BINDINGS = [
        ("q", "back", "Back"),
        ("space", "toggle", "Select"),
        ("a", "select_all", "Select all"),
        ("u", "clear", "Clear"),
        ("b", "export", "Export"),
        ("o", "original", "Full text"),
        ("t", "chinese", "Chinese"),
        ("w", "browser", "Browser"),
        ("r", "refresh", "Refresh"),
        ("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", show=False),
        Binding("left", "back", priority=True, show=False),
        Binding("right", "open", priority=True, show=False),
    ]

    def __init__(self, feedurl: str):
        super().__init__()
        self.feedurl = feedurl
        self.rows: dict[str, sqlite3.Row | dict] = {}   # str(id) -> row (a dict once read)
        self.terms: tuple[str, ...] = ()     # casefolded filter words, all must match; () = off

    def compose(self) -> ComposeResult:
        table = DataTable(cursor_type="row", zebra_stripes=True)
        table.add_columns("Sel", "New", "Date", "Title")
        yield table
        box = Input(placeholder="filter titles — enter keeps it, esc clears it", id="filter")
        box.display = False
        yield box
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()
        self.load()

    def load(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.rows.clear()
        for r in items_of(self.app.conn, self.feedurl):
            title = r["title"].casefold()
            if any(t not in title for t in self.terms):
                continue                             # filtered out: not in rows either, so
            self.rows[str(r["id"])] = r              # a / u / b see only the visible set
            table.add_row(*self.cells(r), key=str(r["id"]))

    def cells(self, r) -> tuple:
        picked = r["guid"] in self.app.selected
        return (Text("[x]" if picked else "[ ]", style=MARK if picked else READ),
                Text("N" if r["unread"] else " ", style=UNREAD if r["unread"] else READ),
                Text(time.strftime("%m-%d", time.localtime(r["pubDate"])), style=READ),
                Text(r["title"], style=UNREAD if r["unread"] else READ))

    def current(self) -> sqlite3.Row | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        return self.rows[key.value]

    def repaint(self, r) -> None:
        table = self.query_one(DataTable)
        for col, cell in zip(table.ordered_columns, self.cells(r)):
            table.update_cell(str(r["id"]), col.key, cell)

    # -- selection --
    def action_toggle(self) -> None:
        if (r := self.current()) is None:
            return
        if r["guid"] in self.app.selected:
            del self.app.selected[r["guid"]]
        else:
            self.app.selected[r["guid"]] = (r["url"], r["title"])
        self.repaint(r)
        table = self.query_one(DataTable)
        table.move_cursor(row=table.cursor_row + 1)   # holding space ticks a run

    def action_select_all(self) -> None:
        """Select everything here; press again when all are selected to deselect."""
        all_in = all(r["guid"] in self.app.selected for r in self.rows.values())
        for r in self.rows.values():
            if all_in:
                self.app.selected.pop(r["guid"], None)
            else:
                self.app.selected[r["guid"]] = (r["url"], r["title"])
            self.repaint(r)

    def action_clear(self) -> None:
        self.app.selected.clear()
        for r in self.rows.values():
            self.repaint(r)

    def action_export(self) -> None:
        """Tick the cursor row when nothing is selected, then export: a failed
        start leaves it selected for a retry, like any other row."""
        if not self.app.selected and (r := self.current()):
            self.app.selected[r["guid"]] = (r["url"], r["title"])
        self.app.export_selected()
        for row in self.rows.values():
            self.repaint(row)

    # -- reading --
    def open_reading(self, mode: str) -> None:
        if (r := self.current()) is None:
            return
        session = ReadingSession(self.app.conn, r, mode)
        if err := session.begin():
            self.notify(f"Could not mark as read: {err}", severity="error")
        else:
            self.rows[str(r["id"])] = session.row      # now a dict, with unread already 0
            self.repaint(session.row)
        self.app.push_screen(StreamScreen(session))

    def action_open(self) -> None:
        self.query_one(DataTable).action_select_cursor()

    def action_refresh(self) -> None:
        self.app.do_refresh()

    # -- title filter --
    def action_filter(self) -> None:
        box = self.query_one(Input)
        box.value = ""                               # each / starts a blank search: leftover
        box.display = True                           # text would concatenate with the next query
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one(Input)
        box.display = False
        if box.value or self.terms:
            box.value = ""
            # Reset and reload here, not only via the queued Changed message: the
            # very next keypress (a, b) must already see the unfiltered rows.
            self.terms = ()
            self.load()
        self.query_one(DataTable).focus()

    def on_input_changed(self, ev: Input.Changed) -> None:
        self.terms = tuple(ev.value.casefold().split())
        self.load()

    def on_input_submitted(self, _) -> None:
        if not self.terms:
            self.query_one(Input).display = False    # enter on an empty box just closes it
        self.query_one(DataTable).focus()

    def check_action(self, action: str, parameters) -> bool:
        """While the filter box has focus only esc may act: left/right are priority
        bindings and would navigate away instead of editing the text."""
        if self.query_one(Input).has_focus:
            return action == "clear_filter"
        return True

    def on_data_table_row_selected(self, _) -> None:     # enter: the RSS summary
        self.open_reading("summary")

    def action_original(self) -> None:                   # o: fetch the full text
        self.open_reading("original")

    def action_chinese(self) -> None:                    # t: fetch, then stream a translation
        self.open_reading("chinese")

    def action_browser(self) -> None:                    # w: hand it to the browser
        if r := self.current():
            import webbrowser
            try:
                grab.check_url(r["url"])         # feed URLs are untrusted; no file://
            except ValueError as e:
                self.notify(str(e), severity="error")
                return
            webbrowser.open(r["url"])

    def action_back(self) -> None:
        self.app.pop_screen()


class FeedsScreen(Screen):
    """Feed list: only feeds scheduled for today that still have something unread."""

    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("r", "refresh", "Refresh"),
        ("b", "export", "Export"),
        ("u", "clear", "Clear"),
        Binding("left", "noop", priority=True, show=False),
        Binding("right", "open", priority=True, show=False),
    ]

    def compose(self) -> ComposeResult:
        table = DataTable(cursor_type="row", zebra_stripes=True)
        table.add_columns("Unread", "Feed")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()
        self.load()

    def on_screen_resume(self) -> None:      # back from the article list: counts changed
        self.load()

    def load(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        counts = feeds_with_unread(self.app.conn)
        saved = {r["rssurl"]: r["title"] for r in self.app.conn.execute(
            "SELECT rssurl,title FROM rss_feed")}
        for url, name in ((f.url, f.name(saved.get(f.url))) for f in self.app.feeds):
            if n := counts.get(url, 0):
                table.add_row(Text(str(n), style=MARK),
                              Text(name, style=UNREAD), key=url)
        if table.row_count == 0:
            table.add_row(Text("-", style=READ), Text("All caught up — press r to refresh", style=READ),
                          key="")

    def on_data_table_row_selected(self, ev) -> None:
        if url := ev.row_key.value:
            self.app.push_screen(ArticlesScreen(url))

    def action_open(self) -> None:
        self.query_one(DataTable).action_select_cursor()

    def action_noop(self) -> None:
        pass

    def action_refresh(self) -> None:
        self.app.do_refresh()

    def action_export(self) -> None:
        self.app.export_selected()

    def action_clear(self) -> None:
        """u here too: a failed-start orphan whose feed no longer shows would
        otherwise be stuck in the selection with no way to drop it."""
        self.app.selected.clear()
        self.notify("Selection cleared")

    def action_quit_app(self) -> None:
        self.app.exit()


class Reader(App):
    """The app. The selection (guid -> (url, title)) lives here so it accumulates
    across feeds."""

    CSS = CSS
    TITLE = "rss"

    def __init__(self, cfg: dict):
        super().__init__()
        self.feeds = todays_feeds(cfg, time.localtime().tm_yday)
        self.conn: sqlite3.Connection | None = None
        self.selected: dict[str, tuple[str, str]] = {}
        self._refresh_lock = threading.Lock()

    def on_mount(self) -> None:
        self.conn = connect()
        self.push_screen(FeedsScreen())
        self.set_interval(RELOAD_MINUTES * 60, self.do_refresh)
        self.do_refresh()

    def export_selected(self) -> None:
        """Hand the accumulated selection to grab, dropping what started; both
        lists bind b here. The UI only starts the work: fetching, translating,
        writing, logging, and notifying all happen in the --bg processes and
        nothing comes back."""
        picked = list(self.selected.items())     # (guid, (url, title)): one order for
        if not picked:                           # the export and the drop below
            self.notify("Nothing selected")
            return
        try:
            errs = grab.export_bg([item for _, item in picked])
        except OSError as e:                     # the log will not open, so nothing started
            self.notify(f"Export could not start: {e}", severity="error")
            return
        # By index, not by comparing (url, title): the same story on two feeds is identical
        for (guid, _), err in zip(picked, errs):
            if err is None:
                del self.selected[guid]          # started, so drop it; failures stay for a retry
        if started := sum(1 for e in errs if e is None):
            self.notify(f"{started} queued for background export")
        if broke := [e for e in errs if e is not None]:
            self.notify(f"{len(broke)} could not start: {broke[0]}", severity="error")

    @work(thread=True, group="refresh", exit_on_error=False)
    def do_refresh(self) -> None:
        """Refresh on its own thread with its own sqlite connection, since a
        connection cannot cross threads.

        One round at a time: a thread worker cannot be cancelled and exclusive=True
        does not stop someone holding r. Two concurrent rounds would both decide an
        article is missing and insert it twice, because guid has no UNIQUE constraint
        and the schema is frozen. A lock is cheaper than a constraint.

        Nothing may escape this function, beyond exit_on_error=False: an exception
        out of a worker kills the whole app."""
        if not self._refresh_lock.acquire(blocking=False):
            return                               # previous round still running
        try:
            fails = []
            report = lambda url, got: isinstance(got, Exception) and fails.append(url)
            conn = connect()
            try:
                fresh = refresh(conn, self.feeds, report)
            finally:
                conn.close()
            msg = f"Refreshed — {fresh} new" if fresh else "Refreshed — nothing new"
            if fails:
                msg += f"; {len(fails)} feed(s) failed"

            def done():
                self.notify(msg, severity="warning" if fails else "information")
                if isinstance(self.screen, (FeedsScreen, ArticlesScreen)):
                    self.screen.load()

            if self.is_running:                  # user already quit: skip the callback
                self.call_from_thread(done)
        except Exception as e:
            if self.is_running:
                self.call_from_thread(
                    lambda: self.notify(f"Refresh failed: {e}", severity="error"))
        finally:
            self._refresh_lock.release()


def main() -> None:
    import fcntl

    grab.STATE.mkdir(parents=True, exist_ok=True)
    lock = (grab.STATE / "reader.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Reader is already running (another terminal?). "
                 "Two instances on one sqlite file end badly, so this one exits.")
    cfg = grab.load_config()
    if not cfg.get("feeds"):
        sys.exit("No feeds in the config — add some RSS URLs first")
    Reader(cfg).run()
    lock.close()                                 # the lock dies with the process anyway


if __name__ == "__main__":
    main()
