#!/usr/bin/env python3
"""Generate the three README screenshots (SVG):

    .venv/bin/python docs/shots.py

Seeds a throwaway sqlite db with canned data — never touches
~/.local/state/grab/cache.db and never hits the network, so the shots can be
regenerated any time instead of depending on what is on HN today. The reading
shot stubs grab.extract with a fixed article: the render pipeline is real, the
article is not.

Dates come from a fixed epoch in a fixed timezone, so rerunning this produces
byte-identical SVGs instead of a diff every time the clock crosses midnight.
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["GRAB_CONFIG"] = str(HERE.parent / "config.toml")
os.environ["TZ"] = "UTC"                    # the date column reads localtime
time.tzset()

import grab    # noqa: E402
import reader  # noqa: E402

DAY = 86400
EPOCH = 1787659200                            # 2026-08-25 12:00:00 UTC

# (feed url, display name, [(title, days ago, summary html)])
SAMPLE = [
    ("https://news.ycombinator.com/rss", "Hacker News", [
        ("Reticulum - a cryptography-based networking stack", 0.1,
         "<p>Pushes crypto and addressing down to the link layer, so anything can carry it: "
         "LoRa, packet radio, a serial cable."),
        ("AGI-64 brings Sierra adventures to the Commodore 64", 0.3,
         "<p>A Sierra AGI interpreter on the 6502, paging resources in from disk."),
        ("The life and death of Direct File", 1.2,
         "<p>The IRS built a free tax filer, then killed it two years in."),
        ("Show HN: a terminal RSS reader in 700 lines", 1.6,
         "<p>No plugin system, no configuration DSL."),
        ("Why is the Rust compiler so slow?", 2.4, "<p>One profiling trip."),
        ("Everything I know about SSH", 3.1, "<p>From keys to ProxyJump."),
    ]),
    ("https://lobste.rs/rss", "Lobsters", [
        ("Rhombus 1.1 is now available", 0.2, "<p>A new surface syntax on top of Racket."),
        ("A pipeline made of garden hoses", 1.1, "<p>What ETL by shell pipe actually costs."),
        ("Notes on distributed systems for young bloods", 2.8, "<p>Worth a reread."),
    ]),
    ("https://jvns.ca/atom.xml", "Julia Evans", [
        ("How to use git bisect", 0.9, "<p>Bisect is not just binary search: it runs scripts."),
        ("Some tiny personal programs", 4.0, "<p>The ones only you will ever run."),
    ]),
    ("https://danluu.com/atom.xml", "Dan Luu", [
        ("Files are hard", 5.0, "<p>Writing a file correctly is harder than you think."),
    ]),
]

# The body for the reading shot. Fetching a real page would need the network and
# would look different every run.
ARTICLE = """\
RSS summaries are close to useless. An HN item is a title and a link; Lobsters gives
you one sentence. Deciding whether something is worth reading means leaving the reader.

## So the fetching moved into the reader

`o` pulls the full text from the original site and renders it in place. `t` fetches,
then streams a Chinese translation. Both keys take the same path:

```python
def producer(self):
    mode, stop, r = self.mode, self.stop, self.row
```

Extraction is [trafilatura](https://trafilatura.readthedocs.io/) to markdown; rendering is
Textual's `MarkdownStream`, which happily renders an unclosed code fence.

| key   | what it shows    | network |
|-------|------------------|---------|
| enter | RSS summary      | no      |
| t     | full text, in zh | yes     |
"""


def seed(db: Path) -> None:
    conn = reader.connect(db)
    for url, title, items in SAMPLE:
        conn.execute("INSERT INTO rss_feed (rssurl, url, title) VALUES (?,?,?)", (url, url, title))
        for n, (headline, ago, content) in enumerate(items):
            conn.execute(
                "INSERT INTO rss_item (guid, title, author, url, feedurl, pubDate, content, unread)"
                " VALUES (?,?,?,?,?,?,?,1)",
                (f"{url}#{n}", headline, "", f"https://example.com/{n}", url,
                 EPOCH - int(ago * DAY), content))
    conn.commit()
    conn.close()


async def shoot(cfg: dict, size, filename: str, script) -> None:
    app = reader.Reader(cfg)
    app.do_refresh = lambda: None          # refresh is the only path that goes out; shots stay offline
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await script(pilot)
        app.save_screenshot(path=str(HERE), filename=filename)


async def feeds(pilot):
    pass


async def articles(pilot):
    await pilot.press("right")             # into Hacker News
    await pilot.pause()
    for _ in range(3):                     # tick three, showing the cursor auto-advance
        await pilot.press("space")
    await pilot.pause()


async def reading(pilot):
    await pilot.press("right")
    await pilot.pause()
    for _ in range(3):                     # land on the row ARTICLE is about, so the title matches
        await pilot.press("down")
    await pilot.press("o")                 # full text; extract is stubbed with ARTICLE
    for _ in range(60):                    # let the typewriter drain
        await asyncio.sleep(0.05)
        await pilot.pause()


def main() -> None:
    grab.extract = lambda url: ("Show HN: a terminal RSS reader in 700 lines", ARTICLE)
    cfg = {"feeds": [f'{url} "~{name}"' for url, name, _ in SAMPLE]}
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "cache.db"
        reader.DB = db
        seed(db)
        for size, name, script in (((96, 11), "shot-feeds.svg", feeds),
                                   ((96, 13), "shot-articles.svg", articles),
                                   ((96, 37), "shot-reading.svg", reading)):
            asyncio.run(shoot(cfg, size, name, script))
            print(HERE / name)


if __name__ == "__main__":
    main()
