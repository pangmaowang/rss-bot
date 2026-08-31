#!/usr/bin/env python3
"""Run `python test_grab.py`. It prints ok when nothing is broken. Never goes online.

Chinese strings in the fixtures are deliberate: they exercise the CJK paths
(zh_punct, slugify, the translation flow).
"""
import os
import tempfile
from pathlib import Path

import grab


def test_config():
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "c.toml"
        cfg.write_text('out = "~/notes"\nfeeds = ["https://x/rss"]\n', encoding="utf-8")
        os.environ["GRAB_CONFIG"] = str(cfg)
        loaded = grab.load_config()
        assert loaded["feeds"] == ["https://x/rss"]

        # out: $GRAB_OUT, then the config, then ./articles, with ~ expanded
        os.environ.pop("GRAB_OUT", None)
        assert grab.out_dir(loaded) == Path(os.path.expanduser("~/notes"))
        os.environ["GRAB_OUT"] = "/tmp/override"
        assert grab.out_dir(loaded) == Path("/tmp/override")
        os.environ.pop("GRAB_OUT")
        assert grab.out_dir({}) == Path("articles")

        # Pointing at a missing config is a typo: fail, do not fall back silently
        os.environ["GRAB_CONFIG"] = str(Path(d) / "nope.toml")
        try:
            grab.load_config()
            assert False, "should exit"
        except SystemExit:
            pass
        os.environ.pop("GRAB_CONFIG")

    # No feeds means there is nothing to open
    try:
        grab.start({})
        assert False, "should exit"
    except SystemExit:
        pass


def test_stream():
    """Streaming translation: incremental selection, error semantics, and cleanup."""
    from unittest import mock

    # No key means no translation. Say so rather than spinning silently.
    with mock.patch.object(grab, "api_key", return_value=None):
        try:
            list(grab.translate_stream("原文"))
            assert False, "a missing key should raise RuntimeError"
        except RuntimeError as e:
            assert "GRAB_API_KEY" in str(e), e

    # Key lookup: the environment wins, otherwise the .env next to the config
    with mock.patch.dict(os.environ, {"GRAB_API_KEY": "sk-env"}):
        assert grab.api_key() == "sk-env"
    # The unfilled placeholder is not a key on the environment path either:
    # `source .env` exports it verbatim
    with mock.patch.dict(os.environ, {"GRAB_API_KEY": "sk-or-v1-"}), \
         mock.patch.object(grab, "config_paths", return_value=[]):
        assert grab.api_key() is None
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".env").write_text(
            '# comment line\nOTHER=x\nGRAB_API_KEY="sk-from-file"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(grab, "config_paths", return_value=[Path(d) / "config.toml"]):
            os.environ.pop("GRAB_API_KEY", None)
            assert grab.api_key() == "sk-from-file"        # quotes stripped, other keys skipped
        # A copied-but-unfilled .env.example must read as "no key" so the "needs
        # GRAB_API_KEY" message fires, not a provider 401. Copy the real file, so
        # this breaks if the example and the guard in api_key() ever drift apart;
        # the bare "sk-or-v1-" line covers .envs copied from the older example.
        example = (Path(grab.__file__).parent / ".env.example").read_text(encoding="utf-8")
        for unfilled in (example, "GRAB_API_KEY=sk-or-v1-\n"):
            (Path(d) / ".env").write_text(unfilled, encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch.object(grab, "config_paths",
                                   return_value=[Path(d) / "config.toml"]):
                os.environ.pop("GRAB_API_KEY", None)
                assert grab.api_key() is None, unfilled
        # No .env means None, and reading the key must not raise on its own
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(grab, "config_paths",
                               return_value=[Path(d) / "nope" / "config.toml"]):
            os.environ.pop("GRAB_API_KEY", None)
            assert grab.api_key() is None

    # Take content deltas only; skip the usage-only tail and empty deltas
    def chunk_ev(text=None, no_choices=False):
        if no_choices:
            return mock.MagicMock(choices=[])                 # usage-only tail packet
        return mock.MagicMock(choices=[mock.MagicMock(delta=mock.MagicMock(content=text))])

    client = mock.MagicMock()
    client.chat.completions.create.return_value = iter(
        [chunk_ev("中文"), chunk_ev(None), chunk_ev(no_choices=True), chunk_ev("标题")])
    with mock.patch("openai.OpenAI", return_value=client), \
         mock.patch.object(grab, "api_key", return_value="sk-test"):
        assert "".join(grab.translate_stream("原文")) == "中文标题"
    kw = client.chat.completions.create.call_args[1]
    assert kw["stream"] is True and kw["messages"][1]["content"].startswith("<article>")

    # SDK errors become RuntimeError: export catches that to fall back to the original
    import openai
    boom = mock.MagicMock()
    boom.chat.completions.create.side_effect = openai.APIConnectionError(request=mock.MagicMock())
    with mock.patch("openai.OpenAI", return_value=boom), \
         mock.patch.object(grab, "api_key", return_value="sk-test"):
        try:
            list(grab.translate_stream("原文"))
            assert False, "should raise RuntimeError"
        except RuntimeError as e:
            assert "Translation failed" in str(e), e

    # Leaving while blocked on the first token must close the stream, not keep billing
    import threading as th_

    class Hanging:
        def __init__(self):
            self.entered, self.closed = th_.Event(), th_.Event()

        def __iter__(self):
            return self

        def __next__(self):
            self.entered.set()
            if self.closed.wait(5):              # closed under us, which is how httpx fails
                raise RuntimeError("stream closed")
            raise StopIteration

        def close(self):
            self.closed.set()

    hang = Hanging()
    hung_client = mock.MagicMock()
    hung_client.chat.completions.create.return_value = hang
    stop = th_.Event()
    with mock.patch("openai.OpenAI", return_value=hung_client), \
         mock.patch.object(grab, "api_key", return_value="sk-test"):
        out = []

        def drain():
            out.extend(grab.translate_stream("原文", stop))

        worker = th_.Thread(target=drain, daemon=True)
        worker.start()
        assert hang.entered.wait(5), "never blocked on the first token"
        stop.set()
        worker.join(5)
        assert not worker.is_alive(), "stream not closed after stop; producer still blocked"
    assert hang.closed.is_set()
    assert out == []                             # closing it ourselves is not an error

    # Nothing at all is a failure, not an empty translation
    empty_client = mock.MagicMock()
    empty_client.chat.completions.create.return_value = iter([])
    with mock.patch("openai.OpenAI", return_value=empty_client), \
         mock.patch.object(grab, "api_key", return_value="sk-test"):
        try:
            list(grab.translate_stream("原文"))
            assert False, "should raise RuntimeError"
        except RuntimeError:
            pass

    # Punctuation runs per whole line, never inside a fence, and fragments rejoin
    chunks = ["你好,世界\n``", "`py\nx = 1  # 注释,别动\n``", "`\n收尾,完"]
    assert "".join(grab.punct_lines(chunks)) == "你好，世界\n```py\nx = 1  # 注释,别动\n```\n收尾，完"

    # Half-width to full-width on Chinese lines only; code, URLs, and numbers untouched
    for src, want in [
            ("值得注意的是,如果库没有依赖,就能用。", "值得注意的是，如果库没有依赖，就能用。"),
            ("先跑 `npm install a, b` 再说,然后看。", "先跑 `npm install a, b` 再说，然后看。"),
            ("见 https://a.com/?a=1,b=2 这链接,注意。", "见 https://a.com/?a=1,b=2 这链接，注意。"),
            ("有 1,000 个文件,都要处理。", "有 1,000 个文件，都要处理。"),
            ("> Source: https://jvns.ca/x", "> Source: https://jvns.ca/x"),   # the colon identifies the header
            ("Run npm install, then check", "Run npm install, then check"),  # pure English untouched
            ("```js\nconst a = [1, 2];\n```", "```js\nconst a = [1, 2];\n```")]:
        assert grab.zh_punct(src) == want, (src, grab.zh_punct(src))

    # A stray fence has to go, or everything after it renders as code
    stray = "正文一。\n\n```\n\n多出来的那个围栏在上面\n\n```py\nx = 1\n```\n\n收尾。"
    fixed = grab.fix_fences(stray)
    assert fixed.count("```") == 2, fixed
    assert "多出来的那个围栏在上面" in fixed and "x = 1" in fixed
    # A balanced document must come through untouched
    ok = "正文。\n\n```py\nx = 1\n```\n\n收尾。"
    assert grab.fix_fences(ok) == ok
    assert grab.fix_fences("没有围栏的文章。") == "没有围栏的文章。"


def test_reader():
    """Storage and fetching: guid compatibility, read state, 304 writes nothing."""
    from unittest import mock

    import reader

    # feeds line parsing, the one place the config string is interpreted
    F = reader.Feed
    for line, want in [
            ("https://a/f", F("https://a/f")),
            ('https://a/f "~我的 源"', F("https://a/f", "我的 源")),
            ('https://a/f "~我的 源" ai eng', F("https://a/f", "我的 源", ("ai", "eng"))),
            ("https://a/f ai", F("https://a/f", None, ("ai",))),
            ("   ", F("")),                      # invalid line: no url
            ('"" ai', F("", None, ("ai",))),      # empty url plus a tag: do not read the tag as the url
    ]:
        assert reader.parse_feed_line(line) == want, line
    try:
        reader.parse_feed_line('https://a/f "~unclosed quote')   # a typo must be reported
        assert False, "unbalanced quotes should exit"
    except SystemExit as e:
        assert "feeds" in str(e)
    # Display name: config alias, then the feed's own title, then the URL
    assert F("u", "别名").name("库里的标题") == "别名"
    assert F("u").name("库里的标题") == "库里的标题"
    assert F("u").name(None) == "u"

    # The four-level guid fallback, byte for byte with newsboat's get_guid
    assert reader.entry_guid({"id": "G", "link": "L", "published": "P"}) == "G"
    assert reader.entry_guid({"link": "L", "published": "Tue, 11 Aug"}) == "LTue, 11 Aug"
    assert reader.entry_guid({"link": "L"}) == "L"
    assert reader.entry_guid({"title": "T"}) == "T"
    assert reader.entry_guid({}) == ""

    class Parsed(dict):
        def __init__(self, entries, status=200, **kw):
            super().__init__(**kw)
            self.entries, self.status, self.feed = entries, status, {"title": "F"}

    conn = reader.connect(":memory:")
    # A new item lands unread and attached to the right feed
    assert reader.store_feed(conn, "F1", "源一", Parsed([{"id": "g1", "title": "一", "link": "u1"}])) == 1
    row = conn.execute("SELECT id, unread, feedurl FROM rss_item WHERE guid='g1'").fetchone()
    assert (row["unread"], row["feedurl"]) == (1, "F1")
    # Refetching a read item must not revive or duplicate it; new items still arrive
    reader.mark_read(conn, row["id"])
    assert reader.store_feed(conn, "F1", "源一",
                             Parsed([{"id": "g1", "title": "一"}, {"id": "g2", "title": "二"}])) == 1
    assert tuple(conn.execute("SELECT count(*), sum(unread) FROM rss_item").fetchone()) == (2, 1)
    # 304 writes nothing
    before = conn.total_changes
    assert reader.store_feed(conn, "F1", "源一", Parsed([], status=304)) == 0
    assert conn.total_changes == before
    # Atom without <id>: W3CDTF dates convert through newsboat's w3cdtf_to_rfc822 first
    import feedparser as fp
    atom = """<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><title>t</title>
<entry><title>A</title><link href='http://ex/a'/><published>2026-08-19T10:00:00Z</published></entry>
<entry><title>B</title><link href='http://ex/b'/><updated>2026-08-19T10:00:00Z</updated></entry>
</feed>"""
    ea, eb = fp.parse(atom).entries
    assert not ea.get("id") and not eb.get("id")
    assert reader.entry_guid(ea) == "http://ex/aWed, 19 Aug 2026 10:00:00 +0000", reader.entry_guid(ea)
    assert reader.entry_guid(eb) == "http://ex/bWed, 19 Aug 2026 10:00:00 +0000", reader.entry_guid(eb)

    # fetch: conditional headers, the 304 short circuit, and 4xx/5xx raising rather
    # than looking exactly like "no new articles"
    import urllib3

    class Resp:
        def __init__(self, status, data=b"", headers=None):
            self.status, self.data, self.headers = status, data, headers or {}

    seen = {}
    def fake(method, url, timeout, headers, retries):
        seen.update(headers)
        seen["retries"] = retries
        return Resp(304)
    with mock.patch.object(urllib3, "request", fake):
        parsed = reader.fetch("https://x/", etag='W/"abc"', lastmodified=1755000000)
    assert parsed.status == 304 and not parsed.entries
    assert seen["If-None-Match"] == 'W/"abc"' and "GMT" in seen["If-Modified-Since"]
    # A dead feed must cost one timeout, not timeout x retries: urllib3's default
    # Retry turns the documented 15s straggler into 60s (measured). Redirects still
    # follow; connect and read failures do not retry on the refresh path.
    assert seen["retries"].connect == 0 and seen["retries"].read == 0
    with mock.patch.object(urllib3, "request",
                           lambda m, u, timeout, headers, retries:
                               seen.update(x_retries=retries) or Resp(500)):
        assert grab.extract("https://x/") is None            # 4xx/5xx -> no article
    # The article path keeps exactly one read retry: a mid-response drop counts as
    # a read error, and for an export a second try beats a lost article.
    assert seen["x_retries"].connect == 0 and seen["x_retries"].read == 1
    with mock.patch.object(urllib3, "request", lambda *a, **k: Resp(503)):
        try:
            reader.fetch("https://x/")
            assert False, "5xx must raise"
        except RuntimeError as e:
            assert "503" in str(e)

    # Concurrent writes: one connection inserting hard while another marks read.
    # Under WAL neither may raise and no read state may be lost.
    import tempfile as tf
    import threading
    wal = Path(tf.mkdtemp()) / "wal.db"
    c1 = reader.connect(wal)
    reader.store_feed(c1, "F", None, Parsed([{"id": f"w{i}", "title": "x"} for i in range(50)]))
    ids = [r[0] for r in c1.execute("SELECT id FROM rss_item")]

    def writer():
        c2 = reader.connect(wal)
        for i in range(30):
            reader.store_feed(c2, "F", None,
                              Parsed([{"id": f"n{i}-{j}", "title": "y"} for j in range(20)]))
        c2.close()

    th = threading.Thread(target=writer)
    th.start()
    for item_id in ids:
        reader.mark_read(c1, item_id)
    th.join()
    assert c1.execute("SELECT count(*) FROM rss_item WHERE unread=0").fetchone()[0] == 50
    c1.close()

    # A failed read-marking write must roll back and raise, never look persisted
    class Locked:
        rolled_back = False

        def execute(self, *args):
            raise reader.sqlite3.OperationalError("database is locked")

        def rollback(self):
            self.rolled_back = True

    locked = Locked()
    try:
        reader.mark_read(locked, 1)
        assert False, "a locked database must not be swallowed"
    except reader.sqlite3.OperationalError:
        pass
    assert locked.rolled_back

    # -- reading session: three modes, read timing, switching invalidates --
    sconn = reader.connect(":memory:")
    sconn.execute("INSERT INTO rss_item (guid,title,author,url,feedurl,pubDate,content,unread)"
                  " VALUES ('s1','标题','','https://a/x','F',1,'<p>摘要在这</p>',1)")
    sconn.commit()
    srow = sconn.execute("SELECT * FROM rss_item WHERE guid='s1'").fetchone()
    with mock.patch.object(reader.grab, "extract", lambda url: ("网页标题", "正文")), \
         mock.patch.object(reader.grab, "translate_stream", lambda doc, stop=None: iter(["译文"])):
        run = lambda pair: "".join(pair[0]())
        s = reader.ReadingSession(sconn, srow)
        assert "摘要在这" in run(s.producer())              # summary comes from the database
        assert s.begin() is None and s.row["unread"] == 0   # opening it marks it read
        assert sconn.execute("SELECT unread FROM rss_item WHERE guid='s1'").fetchone()[0] == 0
        _, before = s.producer()
        s.switch("original")
        _, after = s.producer()
        assert before.is_set() and not after.is_set(), "switching kills the old stop, the new one is clean"
        assert "正文" in run(s.producer())
        s.switch("chinese")
        assert "译文" in run(s.producer())
        # An invalidated producer stops the moment the fetch returns; it must not
        # fire a translation request on behalf of the mode it no longer belongs to
        stale, _ = s.producer()
        s.switch("original")                                # the producer above is now dead
        translated_again = []
        with mock.patch.object(reader.grab, "translate_stream",
                               lambda doc, stop=None: translated_again.append(doc) or iter(["译文"])):
            assert list(stale()) == []
        assert translated_again == [], "a dead producer translated anyway"
        s.end()
        assert s.stop.is_set()
        with mock.patch.object(reader.grab, "extract", lambda url: None):
            s.switch("original")                            # no article text: the error carries the URL
            produce, _ = s.producer()
            try:
                list(produce())
                assert False, "missing article text must raise"
            except RuntimeError as e:
                assert "https://a/x" in str(e)
    # The same race with a real thread blocked inside extract. Translation costs
    # money, so one missed key press here is one article's worth of tokens.
    entered, release, calls = threading.Event(), threading.Event(), []

    def blocking_extract(url):
        calls.append("extract")
        entered.set()
        release.wait(5)
        return "网页标题", "正文"

    with mock.patch.object(reader.grab, "extract", blocking_extract), \
         mock.patch.object(reader.grab, "translate_stream",
                           lambda doc, stop=None: calls.append("translate") or iter(["译文"])):
        racing = reader.ReadingSession(sconn, srow, "chinese")
        produce, _ = racing.producer()
        out = []
        th = threading.Thread(target=lambda: out.extend(produce()), daemon=True)
        th.start()
        assert entered.wait(5), "the producer never reached extract"
        racing.switch("original")                           # mode switched mid-fetch
        release.set()
        th.join(5)
        assert not th.is_alive()
    assert calls == ["extract"], calls                      # stopped after the fetch, no second translation
    assert out == []
    sconn.close()

    class Boom:                                             # read state cannot be written
        def execute(self, *a):
            raise reader.sqlite3.OperationalError("database is locked")

        def rollback(self):
            pass

    failed = reader.ReadingSession(Boom(), dict(srow))
    assert isinstance(failed.begin(), reader.sqlite3.OperationalError)   # handed back for the UI
    assert failed.row["unread"] == 1, "a failed write must not mark the row read"
    assert "摘要在这" in "".join(failed.producer()[0]()), "still readable without the read mark"

    # Invariant: no DELETE anywhere in the reader
    import re as re_
    src = open("reader.py", encoding="utf-8").read() + open("grab.py", encoding="utf-8").read()
    assert not re_.search(r"delete\s+from", src, re_.I), "no DELETE FROM allowed"


def test_reader_ui():
    """Drive the whole UI by key press: selection across feeds, export dispatch,
    read marking, unread filtering, large-list performance, single-instance lock."""
    import asyncio
    import tempfile
    import time as time_
    from unittest import mock

    import reader

    tmp = tempfile.mkdtemp()
    dbp = Path(tmp) / "cache.db"
    conn = reader.connect(dbp)

    def put(feed, i, unread=1):
        conn.execute(
            "INSERT INTO rss_item (guid,title,author,url,feedurl,pubDate,content,unread)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"{feed}-g{i}", f"{feed} 文章{i}", "", f"https://{feed}/a{i}",
             f"https://{feed}/", 1000 + i, "<p>摘要文字</p>", unread))

    for i in range(3):
        put("f1", i)
    for i in range(2):
        put("f2", i)
    for i in range(3045):
        put("big", i)
    for url, title in [("https://f1/", "源一"), ("https://f2/", "库里的源二"),
                       ("https://big/", "大源")]:
        conn.execute("INSERT INTO rss_feed (rssurl,url,title) VALUES (?,?,?)",
                     (url, url, title))
    conn.commit()
    conn.close()

    cfg = {"feeds": ['https://f1/', 'https://f1/',   # duplicated on purpose: dedupe, do not crash
                     'https://f2/ "~源二"', 'https://big/']}
    exported = []
    translated = []

    async def drive():
        app = reader.Reader(cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            feed_table = app.screen.query_one(reader.DataTable)
            assert [feed_table.get_row_at(i)[1].plain for i in range(3)] == ["源一", "源二", "大源"]
            # Feed list: left has nowhere to go so it must not quit; right enters
            feeds_screen = app.screen
            await pilot.press("left")
            assert app.screen is feeds_screen
            await pilot.press("right")
            assert isinstance(app.screen, reader.ArticlesScreen)
            # First feed: space ticks two rows (space also moves down)
            await pilot.press("space", "space")
            assert len(app.selected) == 2, app.selected
            # Back out, into the second feed, tick one more: three across feeds
            await pilot.press("left", "down", "right")
            await pilot.press("space")
            assert len(app.selected) == 3
            # b hands all three to the background and clears the selection
            await pilot.press("b")
            assert {t for _, t in exported} == {"f1 文章2", "f1 文章1", "f2 文章1"}, exported
            assert {u for u, _ in exported} == {"https://f1/a2", "https://f1/a1", "https://f2/a1"}
            assert not app.selected
            # a selects all, pressing it again deselects
            await pilot.press("a")
            assert len(app.selected) == 2
            # Partial failure: the ones that started leave the selection, the rest stay
            # for a retry, so retrying does not export the successful ones twice
            first = list(app.selected.values())[0]
            with mock.patch.object(reader.grab, "export_bg",
                                   lambda items: [OSError("nope")] + [None] * (len(items) - 1)):
                await pilot.press("b")
            assert list(app.selected.values()) == [first], app.selected
            # Same story on two feeds: identical (url, title), so only the index tells them apart
            app.selected.clear()
            app.selected["g-a"] = ("https://same/x", "同一篇")
            app.selected["g-b"] = ("https://same/x", "同一篇")
            with mock.patch.object(reader.grab, "export_bg",
                                   lambda items: [None, OSError("nope")]):
                await pilot.press("b")
            assert list(app.selected) == ["g-b"], app.selected
            app.selected.clear()
            await pilot.press("a")
            await pilot.press("a")
            assert not app.selected
            # b works from the feed list too: the selection accumulates across
            # feeds, so backing out must not strand it
            await pilot.press("a")
            assert len(app.selected) == 2
            await pilot.press("left")
            assert isinstance(app.screen, reader.FeedsScreen)
            exported.clear()
            await pilot.press("b")
            assert {t for _, t in exported} == {"f2 文章0", "f2 文章1"}, exported
            assert not app.selected
            feed_notes = []
            with mock.patch.object(type(app), "notify",
                                   lambda self, msg, **kw: feed_notes.append(str(msg))):
                await pilot.press("b")                   # empty selection: say so
            assert any("Nothing selected" in n for n in feed_notes), feed_notes
            # u clears from the feed list too: a failed-start orphan must not be
            # stuck once its feed no longer shows
            app.selected["orphan"] = ("https://gone/x", "孤儿")
            await pilot.press("u")
            assert not app.selected
            await pilot.press("down", "right")           # back into the second feed
            assert isinstance(app.screen, reader.ArticlesScreen)
            # No selection: b exports the cursor row, and a failed start leaves
            # it ticked for a retry like any other row
            exported.clear()
            await pilot.press("b")
            assert len(exported) == 1 and not app.selected, (exported, app.selected)
            with mock.patch.object(reader.grab, "export_bg", lambda items: [OSError("nope")]):
                await pilot.press("b")
            assert len(app.selected) == 1, app.selected
            await pilot.press("u")                       # drop the retry candidate
            assert not app.selected
            # r in the article list runs the same full refresh
            manual_refresh = []
            with mock.patch.object(app, "do_refresh", lambda: manual_refresh.append(1)):
                await pilot.press("r")
            assert manual_refresh == [1]
            # A failed write still opens the article, but the list must not show it read
            articles = app.screen
            row = articles.current()
            with mock.patch.object(reader, "mark_read",
                                   side_effect=reader.sqlite3.OperationalError("locked")):
                await pilot.press("right")
            assert isinstance(app.screen, reader.StreamScreen)
            assert articles.rows[str(row["id"])]["unread"] == 1
            await pilot.press("left")
            # Right opens the summary and marks it read; left goes back
            await pilot.press("right")
            assert isinstance(app.screen, reader.StreamScreen)
            await pilot.pause(0.3)
            await pilot.press("left")                 # back to the article list
            # The reading screen answers o and t too: press o, then t, without going
            # back first. Modes swap on the same screen and must not leave residue.
            async def wait_for(text, screen):
                for _ in range(60):
                    await pilot.pause(0.05)
                    if text in screen.query_one(reader.Markdown).source:
                        return True
                return False

            await pilot.press("o")
            reading = app.screen
            assert isinstance(reading, reader.StreamScreen)
            assert await wait_for("正文", reading), "the full text never rendered"
            await pilot.press("t")
            assert await wait_for("译文", reading), "the translation never rendered"
            assert translated == ["t"]
            assert app.screen is reading, "switching modes must not push another screen"
            assert reading.session.mode == "chinese"
            body = reading.query_one(reader.Markdown).source
            assert "正文" not in body, body            # the old mode's output is void
            # Fast switching: o then t with no pause leaves only the last mode's content
            await pilot.press("o")
            await pilot.press("t")
            assert await wait_for("译文", reading), "no translation after fast switching"
            await pilot.pause(0.3)
            body = reading.query_one(reader.Markdown).source
            assert body.count("译文") == 1 and "正文" not in body, body
            # o, t, then left in quick succession while the fetch is still out: must not kill the app
            slow = lambda url: (time_.sleep(0.4), ("标题", "正文"))[1]
            notes = []
            with mock.patch.object(reader.grab, "extract", slow), \
                 mock.patch.object(type(app), "notify",
                                   lambda self, msg, **kw: notes.append((msg, kw.get("severity")))):
                await pilot.press("o")
                await pilot.pause(0.05)
                await pilot.press("t")
                await pilot.pause(0.05)
                await pilot.press("left")
                await pilot.pause(0.9)
            assert app.is_running, "fast o/t/left killed the reader"
            # And no silent failure either: rendering onto a detached screen would notify
            assert [n for n in notes if n[1] == "error"] == [], notes
            assert isinstance(app.screen, reader.ArticlesScreen), app.screen
            await pilot.press("right")
            reading = app.screen
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            assert reading.session.stop.is_set(), "leaving must stop the producer, not keep burning tokens"
            # A consumer-side render error must notify exactly once, not blank the page
            async def boom_stream(*a, **kw):
                raise RuntimeError("渲染炸了")

            crashed = []
            with mock.patch.object(reader.grab, "stream_md", boom_stream), \
                 mock.patch.object(type(app), "notify",
                                   lambda self, msg, **kw: crashed.append((msg, kw.get("severity")))):
                await pilot.press("right")
                assert isinstance(app.screen, reader.StreamScreen)
                await pilot.pause(0.2)
                await pilot.press("left")
                await pilot.pause()
            assert [n for n in crashed if n[1] == "error"] == [("Render error: 渲染炸了", "error")], crashed

            # Streaming must not drag the view to the bottom: reading starts at
            # the top, the stream fills in below
            with mock.patch.object(reader.grab, "extract",
                                   lambda url: ("标题", "很长的正文段落。\n\n" * 120)):
                await pilot.press("o")
                reading = app.screen
                assert isinstance(reading, reader.StreamScreen)
                for _ in range(120):                     # let the stream finish
                    await pilot.pause(0.05)
                    if reading.query_one(reader.Markdown).source.count("很长的正文段落") >= 120:
                        break
                else:
                    assert False, "the stream never finished"
                scroll = reading.query_one(reader.VerticalScroll)
                assert scroll.max_scroll_y > 0, "content too short to prove anything"
                assert scroll.scroll_offset.y == 0, scroll.scroll_offset
                await pilot.press("left")

            # Performance: time opening the 3045-row feed
            await pilot.press("left", "down", "down", "right")
            t0 = time_.perf_counter()
            await pilot.pause()
            big_open = time_.perf_counter() - t0
            table = app.screen.query_one(reader.DataTable)
            assert table.row_count == 3045
            assert big_open < 1.0, f"the large feed took {big_open:.2f}s to open"

            # / filters by title as you type, case-insensitively
            await pilot.press("/")
            assert isinstance(app.screen.focused, reader.Input)
            await pilot.press(*"300")
            want = sum(1 for i in range(3045) if "300" in f"big 文章{i}")
            assert 0 < want < 3045                   # the case discriminates
            assert table.row_count == want, (table.row_count, want)
            # left while typing edits the input; it must not leave the screen
            articles_screen = app.screen
            await pilot.press("left")
            assert app.screen is articles_screen
            # enter keeps the filter and hands focus back to the list
            await pilot.press("enter")
            assert app.screen.focused is table
            assert table.row_count == want
            # a operates on the filtered view: / + a + b exports one keyword
            await pilot.press("a")
            assert len(app.selected) == want
            await pilot.press("a")                   # press again: deselect them
            assert not app.selected
            # esc clears the filter and everything comes back
            await pilot.press("escape")
            assert table.row_count == 3045
            # esc while still typing must work too: it is the one action the
            # check_action allow-list keeps open while the box has focus
            await pilot.press("/")
            await pilot.press(*"300")
            assert table.row_count == want
            await pilot.press("escape")
            assert table.row_count == 3045
            assert app.screen.focused is table
            # multi-word: space-separated terms, every one must match, any order
            await pilot.press("/")
            await pilot.press("3", "0", "space", "9")
            want2 = sum(1 for i in range(3045)
                        if all(t in f"big 文章{i}" for t in ("30", "9")))
            assert 0 < want2 < 3045                  # the case discriminates
            assert table.row_count == want2, (table.row_count, want2)
            await pilot.press("escape")
            assert table.row_count == 3045
            # / after a kept filter starts a blank search, and esc + an immediate a
            # sees the unfiltered rows (the clear reloads synchronously, not via the
            # queued Changed message)
            await pilot.press("/")
            await pilot.press(*"300", "enter")       # keep the filter
            await pilot.press("/")
            assert app.screen.query_one(reader.Input).value == ""
            await pilot.press("escape", "a")
            assert len(app.selected) == 3045, len(app.selected)
            await pilot.press("a")
            assert not app.selected
        return big_open

    with mock.patch.object(reader, "DB", dbp), \
         mock.patch.object(reader.grab, "STATE", Path(tmp)), \
         mock.patch.object(reader, "refresh", lambda *a, **k: 0), \
         mock.patch.object(reader.grab, "export_bg",
                           lambda items: exported.extend(items) or [None] * len(items)), \
         mock.patch.object(reader.grab, "extract", lambda url: ("标题", "正文")), \
         mock.patch.object(reader.grab, "translate_stream",
                           lambda doc, stop=None: translated.append("t") or iter(["译文"])):
        big_open = asyncio.run(drive())

    # The read mark persisted: one item of the second feed is gone from the count
    conn = reader.connect(dbp)
    assert conn.execute("SELECT sum(unread) FROM rss_item WHERE feedurl='https://f2/'")         .fetchone()[0] == 1
    conn.close()

    # Single-instance lock: main() exits when the lock is held
    import fcntl
    with mock.patch.object(reader.grab, "STATE", Path(tmp)),          mock.patch.object(reader.grab, "load_config", lambda: cfg),          mock.patch.object(reader.Reader, "run", lambda self: None):
        reader.main()                                # takes the lock normally
        holder = (Path(tmp) / "reader.lock").open("w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            reader.main()
            assert False, "a held lock should exit"
        except SystemExit as e:
            assert "already running" in str(e)
        holder.close()
    print(f"  (3045 rows opened in {big_open*1000:.0f}ms)")


def test_stream_md():
    """Streaming presentation, shared by --read --zh and the reader's o/t: normal
    stream, producer exception, empty output, stop. None may hang on the sentinel."""
    import asyncio
    import itertools
    import threading
    import time as time_

    from textual import work
    from textual.app import App
    from textual.widgets import Markdown

    notes = []

    def render(produce, stop=None):
        class T(App):
            def notify(self, message, **kw):     # capture toasts instead of rendering
                notes.append(str(message))

            def compose(self):
                yield Markdown()

            def on_mount(self):
                self.done = False
                self.go()

            @work
            async def go(self):
                await grab.stream_md(self.query_one(Markdown), produce, stop)
                self.done = True

        async def drive():
            app = T()
            async with app.run_test() as pilot:
                for _ in range(120):
                    await pilot.pause(0.05)
                    if app.done:
                        break
                assert app.done, "the stream never ended; a lost sentinel waits forever"
                return app.query_one(Markdown).source
        return asyncio.run(drive())

    # Normal stream: fragments reassemble in order, and no toast fires
    out = render(lambda: iter(["# 标题\n\n", "正文一。\n"]))
    assert "# 标题" in out and "正文一。" in out, out
    assert not notes, notes
    # A producer that dies mid-way shows the error once instead of waiting forever,
    # and ALSO toasts it: the in-document warning lands at the end of the text,
    # below the fold now that reading starts at the top
    def boom():
        yield "开头。\n"
        raise AttributeError("lxml 那边的意外")   # any exception, not only RuntimeError
    out = render(boom)
    assert out.count("⚠️") == 1 and "lxml 那边的意外" in out, out
    assert any("lxml 那边的意外" in n for n in notes), notes
    # Empty output: render nothing, still finish cleanly
    assert render(lambda: iter([])) == ""
    # Stop: nothing more renders and the producer stops, or translation keeps billing
    made = []

    def forever():
        for i in itertools.count():
            made.append(i)
            time_.sleep(0.01)
            yield f"第{i}段。\n"

    stop = threading.Event()
    threading.Timer(0.25, stop.set).start()
    notes.clear()
    out = render(forever, stop)
    assert not notes, notes                       # a stop is not an error: no toast
    n = len(made)
    time_.sleep(0.3)
    assert len(made) <= n + 1, (n, len(made))     # the producer stopped, one tick of slack
    assert "第0段" in out, out                    # whatever rendered before the stop stays


def main():
    test_config()
    test_stream()
    test_stream_md()
    test_reader()
    test_reader_ui()
    # Today's feeds: parse once, rotate by tag, dedupe by url. day % len picks the group.
    import reader as _reader
    cfg_feeds = {"feeds": ["https://hn/rss", 'https://a/f "~A" ai', 'https://e/f "~E" eng',
                           'https://x/f "~The ai digest" eng',   # a bare word in the name is not a tag
                           "   ", 'https://hn/rss "~重复的"'],   # invalid line dropped; duplicate url keeps the first
                 "rotate": ["ai", "eng"]}
    day = lambda n: [f.url for f in _reader.todays_feeds(cfg_feeds, n)]
    assert day(0) == ["https://hn/rss", "https://a/f"], day(0)
    assert day(1) == ["https://hn/rss", "https://e/f", "https://x/f"], day(1)
    assert day(2) == day(0)
    assert _reader.todays_feeds(cfg_feeds, 1)[0].alias is None, "duplicate url: the first one wins"
    # Without rotate every feed shows and tags do not matter
    assert len(_reader.todays_feeds({"feeds": ["https://a/f ai", "https://b/f"]}, 5)) == 2
    # Dedupe happens before rotation, so "first wins" does not depend on the date
    dup = {"feeds": ['https://x/f "~B" ai', 'https://x/f "~A"'], "rotate": ["ai", "eng"]}
    assert [(f.url, f.alias) for f in _reader.todays_feeds(dup, 0)] == [("https://x/f", "B")]
    assert _reader.todays_feeds(dup, 1) == [], "the first line carries ai, so the feed is out on eng days"
    # slugify: keep CJK, collapse punctuation, truncate, fall back on an empty title
    assert grab.slugify("Hello, 世界! (2026)") == "Hello-世界-2026"
    assert len(grab.slugify("啊" * 100)) == 60
    assert grab.slugify("///") == "untitled"
    assert grab.slugify("") == "untitled"

    # HN item pages: mock Algolia, check the post body and comments render with tags stripped
    import io
    import json
    import unittest.mock as mock

    algolia = json.dumps({
        "title": "Ask HN: 测试?", "points": 42, "author": "alice",
        "text": "<p>第一段</p><p>带 <i>斜体</i> 和 <code>code</code></p>", "url": None,
        "children": [
            {"author": "bob", "text": "<p>同意&#x27;观点</p>", "children": []},
            {"author": None, "text": None, "children": []},      # deleted comment, must be skipped
        ],
    })
    with mock.patch("urllib.request.urlopen",
                    return_value=io.StringIO(algolia)) as m:
        title, body = grab.extract("https://news.ycombinator.com/item?id=123")
    assert "items/123" in m.call_args[0][0]
    assert title == "Ask HN: 测试?"
    assert "42 points by alice" in body
    assert "第一段" in body and "斜体" in body
    assert "**bob**: 同意'观点" in body, body      # HTML entities decoded
    assert "<p>" not in body and "<i>" not in body
    # A non-HN host must not hit that branch (extract would go online, so only check the match)
    import re as _re
    assert not _re.match(r"https?://news\.ycombinator\.com/item\?id=(\d+)",
                         "https://example.com/item?id=1")

    # --read --zh routing: translation wraps the preview only, never the saved file.
    # The preview streams through translate_stream and prints piece by piece.
    import sys as _sys
    with mock.patch.object(grab, "extract", return_value=("T", "B")), \
         mock.patch("sys.stdout.isatty", return_value=False), \
         mock.patch.object(grab, "translate_stream",
                           side_effect=lambda s: iter(["译:", s])) as tr, \
         mock.patch.object(_sys, "argv", ["grab.py", "--read", "--zh", "https://a.example/x"]), \
         mock.patch("builtins.print") as pr:
        grab.main()
    assert tr.called
    printed = [c[0][0] for c in pr.call_args_list if c[0]]
    assert printed[:2] == ["译:", "# T\n\n> Source: https://a.example/x\n\n---\n\nB\n"], printed
    assert all(c[1].get("flush") for c in pr.call_args_list if c[0] and c[0][0]), "must flush"
    with mock.patch.object(grab, "extract", return_value=("T", "B")), \
         mock.patch.object(grab, "translate_stream") as tr, \
         mock.patch.object(_sys, "argv", ["grab.py", "--read", "https://a.example/x"]), \
         mock.patch("builtins.print"):
        grab.main()
    assert not tr.called                        # without --zh the translation API is never touched

    # Background export: the UI passes (url, title), one --bg process each, one log file
    spawned = []
    with tempfile.TemporaryDirectory() as d, \
         mock.patch.object(grab, "STATE", Path(d)), \
         mock.patch.object(grab.subprocess, "Popen",
                           lambda argv, **kw: spawned.append((argv, kw)) or mock.MagicMock()):
        grab.export_bg([("https://a/1", "标题一"), ("https://b/2", "标题二")])
        assert (Path(d) / "grab.log").exists()
    assert [argv[2:] for argv, _ in spawned] == [["--bg", "https://a/1", "标题一"],
                                                 ["--bg", "https://b/2", "标题二"]], spawned
    assert all(kw["start_new_session"] for _, kw in spawned), "jobs must outlive the UI"

    # One Popen failing: the rest still start and the failure is reported by index,
    # so a retry does not export the successful ones a second time
    tried = []

    def flaky(argv, **kw):
        tried.append(argv)
        if len(tried) == 2:
            raise OSError("cannot fork")
        return mock.MagicMock()

    with tempfile.TemporaryDirectory() as d, \
         mock.patch.object(grab, "STATE", Path(d)), \
         mock.patch.object(grab.subprocess, "Popen", flaky):
        # Two identical (url, title) on purpose: that is the same story on two feeds
        errs = grab.export_bg([("https://a/1", "一"), ("https://a/1", "一"), ("https://c/3", "三")])
        logged = (Path(d) / "grab.log").read_text(encoding="utf-8")
    assert [e is None for e in errs] == [True, False, True], errs
    assert isinstance(errs[1], OSError)
    assert len(tried) == 3, "one failure must not end the batch"
    assert "✗ Could not start 一" in logged, logged      # failures leave a trace in grab.log too

    # The --bg side: use the title the list gave, write the file, notify, and log the
    # start before the result
    with tempfile.TemporaryDirectory() as d:
        conf = Path(d) / "c.toml"
        conf.write_text('feeds = ["https://x/rss"]\n', encoding="utf-8")   # translate off
        with mock.patch.object(grab, "extract", lambda url: ("网页标题", "正文" * 200)), \
             mock.patch.object(grab, "notify") as notified, \
             mock.patch.dict(os.environ, {"GRAB_CONFIG": str(conf), "GRAB_OUT": d}), \
             mock.patch.object(_sys, "argv",
                               ["grab.py", "--bg", "https://a.example/x", "列表里的标题"]), \
             mock.patch("builtins.print") as pr:
            try:
                grab.main()
                assert False, "main should sys.exit"
            except SystemExit as e:
                assert e.code == 0, e.code
        assert (Path(d) / "列表里的标题.md").exists(), list(Path(d).iterdir())
        logged = [c[0][0] for c in pr.call_args_list if c[0]]
        assert any("▶" in l for l in logged) and any("✓" in l for l in logged), logged
        assert notified.call_args[0][0] == "✓ Exported", notified.call_args

        # No article text: a ✗ line in the log, a notification, and exit code 1
        with mock.patch.object(grab, "extract", lambda url: None), \
             mock.patch.object(grab, "notify") as notified, \
             mock.patch.dict(os.environ, {"GRAB_CONFIG": str(conf), "GRAB_OUT": d}), \
             mock.patch.object(_sys, "argv", ["grab.py", "--bg", "https://a.example/y", "抓不到"]), \
             mock.patch("builtins.print"):
            try:
                grab.main()
                assert False, "main should sys.exit"
            except SystemExit as e:
                assert e.code == 1
        assert notified.call_args[0][0] == "✗ No article text", notified.call_args

    # The old four-argument form is gone: extra arguments are a typo, not something to eat
    with mock.patch.object(_sys, "argv",
                           ["grab.py", "--bg", "https://a/x", "标题", "", "源名"]):
        try:
            grab.main()
            assert False, "too many args should exit"
        except SystemExit as e:
            assert "Too many arguments" in str(e), e

    with tempfile.TemporaryDirectory() as d:
        out = Path(d)
        grab.extract = lambda url: ("同一个标题", "正文" * 200)

        p1 = grab.grab("https://a.example/1", out)
        p2 = grab.grab("https://b.example/2", out)
        # Same title, no overwrite: both files exist and point at their own source
        assert p1 != p2, (p1, p2)
        assert p1.exists() and p2.exists()
        assert "https://a.example/1" in p1.read_text(encoding="utf-8")
        assert "https://b.example/2" in p2.read_text(encoding="utf-8")

        # Parallel --bg race: unique() said the name was free but another process
        # created it first; the exclusive create must take the next name, not
        # overwrite the winner's file
        taken = out / "抢先.md"
        taken.write_text("先到的", encoding="utf-8")
        with mock.patch.object(grab, "unique", side_effect=[taken, out / "抢先-2.md"]):
            pr = grab.grab("https://r.example/9", out, title="抢先")
        assert pr == out / "抢先-2.md", pr
        assert taken.read_text(encoding="utf-8") == "先到的"

        # Header format: this is what a markdown reader shows as title and back link
        head = p1.read_text(encoding="utf-8").split("---")[0]
        assert head == "# 同一个标题\n\n> Source: https://a.example/1\n\n", repr(head)

        # A caller-supplied title wins, matching what the list showed
        p3 = grab.grab("https://c.example/3", out, title="列表里的标题")
        assert p3.name == "列表里的标题.md", p3.name

        # translate=true saves the translation and names the file after its H1. The
        # model does move the H1 below the source line, so use a reordered fixture.
        with mock.patch.object(grab, "translate", side_effect=lambda d:
                               "> Source: https://e.example/5\n\n---\n\n# 中文标题\n\n译文"):
            p4 = grab.grab("https://e.example/5", out, title="English Title", zh=True)
        assert p4.name == "中文标题.md", p4.name
        got = p4.read_text(encoding="utf-8")
        assert got == "# 中文标题\n\n> Source: https://e.example/5\n\n---\n\n译文\n", repr(got)

        # Only the single leading H1 belongs to the header: a heading that opens
        # the body must survive the rebuild, not vanish into the eaten run
        with mock.patch.object(grab, "translate", side_effect=lambda d:
                               "# 标题甲\n\n> Source: https://g.example/7\n\n---\n\n# 引言\n\n正文"):
            p6 = grab.grab("https://g.example/7", out, title="X", zh=True)
        assert p6.name == "标题甲.md", p6.name
        assert p6.read_text(encoding="utf-8") == \
            "# 标题甲\n\n> Source: https://g.example/7\n\n---\n\n# 引言\n\n正文\n", \
            repr(p6.read_text(encoding="utf-8"))

        # A failed translation must not lose the text already fetched: save the original
        with mock.patch.object(grab, "translate", side_effect=RuntimeError("翻译接口挂了")):
            p5 = grab.grab("https://f.example/6", out, title="Fallback", zh=True)
        assert p5.read_text(encoding="utf-8").startswith("# Fallback"), p5.read_text()

        # No article text gives None, not an exception
        grab.extract = lambda url: None
        assert grab.grab("https://d.example/4", out) is None

        # Anything but http(s) is refused: feed URLs are untrusted and file:// reads local files
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x/y", "/etc/passwd"):
            try:
                grab.grab(bad, out)
                assert False, f"should refuse {bad}"
            except ValueError:
                pass

    print("ok")


if __name__ == "__main__":
    main()
