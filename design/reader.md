# reader.py design notes

`reader.py` replaced a vendored newsboat fork (37.5k lines of C++, 15.9k of Rust,
a hand-rolled build script for the dead stfl library) that existed to carry a
150-line multi-select patch. The reader, the fetcher, and the translator are now
one language and one process.

The rewrite replaced code, not data. `cache.db` kept its schema, `config.toml`
kept its syntax, and the day-to-day key bindings kept their meaning.

## Layers

`reader.py` is three layers in one file:

- **storage**: stdlib `sqlite3` over `~/.local/state/grab/cache.db`, four
  queries: list a feed's items, insert new items by guid, flip `unread`, save
  `etag`/`lastmodified`.
- **fetching**: `feedparser` per feed, conditional requests via
  `If-None-Match` / `If-Modified-Since`, up to 8 feeds at a time in a thread
  pool. One feed failing does not end the round.
- **UI**: Textual screens: feed list, article list, reading view. Every
  operation runs in a worker, so the list stays responsive while a page is
  being fetched or translated.

Fetching, translating, streaming, and background export all live in `grab.py`
and are imported here. The CLI and the reader run the same code.

## Invariants

1. **No DELETE anywhere.** Feeds that rotate out, feeds you unsubscribe from,
   old items: the rows stay. This makes newsboat's cleanup bug structurally
   impossible.
2. **`unread` changes in exactly two places.** A new row inserts with 1, and
   reading an article sets it to 0. Newsboat also reset unread when an item's
   content changed; that behaviour is deliberately gone.
3. **The schema is frozen.** No columns added, none changed, unused columns
   (`enclosure_*`, `flags`, `base`) carried along untouched. `journal_mode` is
   WAL, which is a logging change rather than a schema change: the UI
   connection and the refresh thread write concurrently, and in delete mode
   they lock each other out until the app crashes.

## guid compatibility

`entry_guid()` reproduces newsboat's four-level fallback:
`guid -> link + pubDate -> link -> title`. Any change here makes every item
that relied on a fallback key look new, and the whole database comes back
unread on the next refresh.

The pubDate level has to match byte for byte. RSS2 `<pubDate>` is concatenated
raw; Atom `<published>`/`<updated>` and `dc:date` are W3CDTF and get converted
to RFC822 first. `RFC822ISH` decides which is which. Two rare cases still
differ and are accepted: RSS2 feeds carrying a non-RFC822 date, and `<id>`
elements with an `xml:base`.

## Reading view

`o` and `t` swap modes in place rather than pushing a new screen. Switching or
leaving cancels the old producer cooperatively: Python threads cannot be
killed, so a fetch already in flight runs to completion, but its result is
dropped and never translated or rendered. `ReadingSession` owns the current
article, the mode, the read-marking, and the producer lifetime; the screen only
displays and navigates.

## Deliberately absent

Filter expressions, podboat, Google Reader sync, OPML, and the rest of the
newsboat config language. Never used them. `/` search, in-article highlighting,
and per-feed refresh are not here either, but each is a few dozen lines if the
need shows up.

## Known trade-offs

- Preview and export are two independent fetches with no cache between them.
  Caching would cost invalidation logic to save 0.3 to 1 second.
- Exporting a large selection spawns one process per article with no rate
  limit. Fine for tens, split the batch for hundreds.
- Single instance only. Two processes writing one sqlite file is not worth
  supporting.
