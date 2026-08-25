#!/usr/bin/env python3
"""Fetch a web page into clean Markdown. Companion tool to reader.py.

`./grab.py` just works: the lines below re-exec into the repo's .venv,
so there is no need to activate anything and no absolute path in the shebang.

    grab.py start              launch the reader (same as running reader.py)
    grab.py --read <URL>       fetch one page to stdout, no file written
    grab.py --read --zh <URL>  same, translated to Chinese; opens a streaming
                               Textual view in a terminal, plain markdown in a pipe
    grab.py <URL> [title]      fetch one page, print the saved path
    grab.py                    read URLs from stdin, one per line (bulk export)
    grab.py --bg <URL> [title] background export: logs to grab.log, posts a
                               notification when done (this is what `b` uses)

Config lookup: $GRAB_CONFIG -> ~/.config/grab/config.toml -> config.toml next to this file
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
VENV = HERE.parent / ".venv"
if Path(sys.prefix) != VENV and (VENV / "bin" / "python3").exists():
    os.execv(str(VENV / "bin" / "python3"), [str(VENV / "bin" / "python3"), str(HERE), *sys.argv[1:]])

import re
import shutil
import subprocess
import time
import tomllib

# Plenty of sites, including some RSS endpoints, 403 a non-browser UA.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
STATE = Path(os.path.expanduser("~/.local/state/grab"))   # cache.db and grab.log live here


# -- config, startup ---------------------------------------------------

def stamp() -> str:
    """grab.log is the only trace a background fetch leaves, so it needs times."""
    return time.strftime("%m-%d %H:%M:%S ") if "--bg" in sys.argv else ""


def config_paths() -> list[Path]:
    """Search order for the config. api_key() reuses it, since .env sits next to
    the config and two copies of this lookup would drift apart."""
    if env := os.getenv("GRAB_CONFIG"):
        paths = [Path(os.path.expanduser(env))]
        if not paths[0].exists():
            sys.exit(f"$GRAB_CONFIG points at a missing file: {paths[0]}")
        return paths
    return [Path(os.path.expanduser("~/.config/grab/config.toml")), HERE.parent / "config.toml"]


def load_config() -> dict:
    """A $GRAB_CONFIG that points nowhere is a typo, not a reason to fall back."""
    for path in config_paths():
        if path.exists():
            with path.open("rb") as f:
                cfg = tomllib.load(f)
            # Relative paths resolve against the config file, not the cwd: the reader
            # starts from anywhere, and cwd-relative would scatter files everywhere.
            if "out" in cfg:
                cfg["out"] = str(path.parent / os.path.expanduser(cfg["out"]))
            return cfg
    return {}


def out_dir(cfg: dict) -> Path:
    return Path(os.path.expanduser(os.getenv("GRAB_OUT") or cfg.get("out") or "articles"))


def start(cfg: dict) -> None:
    """Launch the reader. The feeds check runs before exec so an empty config fails
    here rather than after the process has been replaced. sys.executable rather than
    the script itself, since we are already inside the venv."""
    if not cfg.get("feeds"):
        sys.exit("No feeds in the config — add some RSS URLs first")
    reader = str(HERE.parent / "reader.py")
    os.execv(sys.executable, [sys.executable, reader])


# -- fetching, markdown ------------------------------------------------

def slugify(title: str, max_len: int = 60) -> str:
    """Title to a safe filename, CJK preserved."""
    s = re.sub(r"[^\w一-鿿]+", "-", title, flags=re.UNICODE).strip("-")
    return (s or "untitled")[:max_len]


def hn_comment_md(html: str) -> str:
    """HN comment HTML to plain text. It only ever contains <p>/<a>/<i>/<code>/<pre>,
    so blank lines for <p> and strip the rest. Not worth a real converter."""
    import lxml.html

    text = lxml.html.fromstring(f"<div>{html.replace('<p>', chr(10) * 2)}</div>").text_content()
    return text.strip()


def hn_item(item_id: str) -> tuple[str, str] | None:
    """Ask/Show HN and comment pages. trafilatura turns HN's table layout into
    navigation and score fragments, so use the Algolia API and build the md here."""
    import json
    from urllib.request import urlopen

    with urlopen(f"https://hn.algolia.com/api/v1/items/{item_id}", timeout=15) as resp:
        item = json.load(resp)
    title = item.get("title") or f"HN item {item_id}"
    parts = [f"{item.get('points') or 0} points by {item.get('author') or '?'}"]
    if item.get("text"):                          # Ask HN and friends: the post is the body
        parts.append(hn_comment_md(item["text"]))
    if item.get("url"):                           # link post: pull the linked article in
        got = extract(item["url"])
        parts.append(got[1] if got else f"Could not fetch the article, open it manually: {item['url']}")
    comments = [c for c in item.get("children") or [] if c.get("text")]
    if comments:
        parts.append("## Comments\n\n" + "\n\n---\n\n".join(
            f"**{c.get('author') or '?'}**: {hn_comment_md(c['text'])}"
            for c in comments[:10]))              # Algolia returns them in HN's ranking order
    return title, "\n\n".join(parts)


def extract(url: str) -> tuple[str, str] | None:
    """(title, markdown body), or None if the fetch fails or the text is too short."""
    import lxml.html
    import trafilatura       # a 265ms import; the foreground `b` path never needs it
    import urllib3

    check_url(url)   # every entry point validates for itself; URLs are untrusted
    if m := re.match(r"https?://news\.ycombinator\.com/item\?id=(\d+)", url, re.I):
        return hn_item(m[1])
    # Not trafilatura.fetch_url: its zstd path chokes on streaming frames, either
    # raising without a content-size or passing compressed bytes straight through.
    # urllib3 decodes gzip/br/zstd as a stream (needs brotli + zstandard installed).
    resp = urllib3.request(
        "GET", url, timeout=30.0,
        headers={"User-Agent": UA})
    if resp.status >= 400:
        return None
    html = resp.data                             # bytes; lxml honours the declared encoding
    # Most in-page links are relative and die once the text leaves the site. lxml also
    # handles <base href> and //host/path, which a regex would miss. trafilatura takes
    # the lxml tree directly rather than reparsing.
    tree = lxml.html.document_fromstring(html)
    tree.make_links_absolute(url, handle_failures="discard")
    body = trafilatura.extract(
        tree, output_format="markdown",
        include_links=True, include_images=True, include_tables=True,
    )
    if not body or len(body) < 200:
        return None  # index and link-list pages yield fragments, not an article
    meta = trafilatura.extract_metadata(html)
    return ((meta.title if meta else None) or url), body


def unique(path: Path) -> Path:
    """Never overwrite: the same story on two feeds produces the same title."""
    n, stem = 2, path.stem
    while path.exists():
        path, n = path.with_name(f"{stem}-{n}{path.suffix}"), n + 1
    return path


def check_url(url: str) -> None:
    """Feed URLs are untrusted. http(s) only, or file:// reads any local file."""
    if not re.match(r"https?://", url, re.I):
        raise ValueError(f"Only http(s) is allowed, got: {url[:60]}")


def document(title: str, url: str, body: str) -> str:
    """One header format for both the saved file and the reading view."""
    return f"# {title}\n\n> Source: {url}\n\n---\n\n{body}\n"


# -- translation -------------------------------------------------------

# Without these rules the model follows English clause structure and produces
# translationese. Each line targets a pattern that showed up in real output.
TRANSLATE_PROMPT = (
    # Full-width punctuation throughout: the model copies the prompt's punctuation
    # style, and telling it not to does not work.
    "把下面的 markdown 文章翻译成简体中文。\n"
    "译文要求：\n"
    "- 说人话。按中文的语序和节奏重新组织句子，别顺着英文的从句结构硬译。"
    "「让我们」「这意味着」「在……的情况下」「进行……的操作」「众所周知」这类翻译腔换成正常说法。\n"
    "- 中文不像英文那样每句都要主语，代词和连接词该省就省；长句拆短。\n"
    "- 技术术语用中文技术圈的通行叫法（cache 叫缓存，repository 叫仓库；commit、pull request、"
    "import map 这种本来就没人翻的，留原文）。首次出现且容易歧义的，写成「中文（English）」"
    "括注一次，后面只用中文。\n"
    "- 人名、公司名、产品名、库名、命令、参数、文件名、报错信息一律留原文，不要音译。\n"
    "- 语气跟着原文走：原文随意就别写成公文，原文严谨就别加口水话。\n"
    "- 正文的标点用全角（，。：；「」），代码、命令、URL 里的标点不动。\n"
    "格式要求：保留全部 markdown 结构（标题层级、链接、图片、表格、引用），"
    "代码块和 URL 原样不动，开头那行 `> Source: <URL>` 原样保留。\n"
    "只输出译文，不要任何说明。\n"
    # The article arrives after the prompt wrapped in <article>. Without saying so,
    # a long rule list makes the model answer "please provide the full article".
    "stdin 里 <article> 标签包着的就是要翻的全文。它可能很短、可能从文章中间开始、"
    "可能只是个片段 —— 都照翻不误，不要反问、不要要求补充，直接输出译文。"
)


def translate_stream(doc: str, stop=None):
    """Translate a whole document, yielding it piece by piece.

    Raises RuntimeError rather than exiting, because export falls back to the
    original text instead of losing the article.

    This once split the document into 8 parallel chunks (71.5s to 13.2s). Removed:
    total time is not the point, a steady stream is, and parallel chunks arrive in
    bursts the typewriter then sprints through. Chunking also made the model invent
    headings, repeat the `> Source:` line, and emit stray fences.

    stop: set it when the user leaves or switches mode and the HTTP stream closes at
    once. Breaking out of the loop is not enough, since a generator blocked on the
    first token has not yielded yet and keeps billing until the 180s timeout."""
    if not (key := api_key()):
        raise RuntimeError("Translation needs GRAB_API_KEY: put it in the .env next to your "
                           "config (see .env.example)")
    yield from translate_api(doc, key, stop)


def api_key() -> str | None:
    """$GRAB_API_KEY first, then the .env next to the config.

    Not config.toml, that file is in git. Not ~/.zshrc either: every process you run
    could then read it, and that file often syncs to a dotfiles repo. The environment
    variable is for one-off overrides. The key only ever travels in an HTTP header,
    never in argv, so it stays out of `ps`."""
    if key := os.getenv("GRAB_API_KEY"):
        return key
    for path in config_paths():
        env = path.parent / ".env"
        if not env.exists():
            continue
        for line in env.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().partition("=")
            if sep and name.strip() == "GRAB_API_KEY":
                return value.strip().strip("\"'") or None
    return None


def translate_api(doc: str, key: str, stop=None):
    """The official OpenAI SDK, pointed at any base_url. Same /chat/completions
    protocol, so OpenRouter, DeepSeek, and a local vLLM all work.

    The SDK is here for two things that are hard to hand-roll: retry with backoff
    (time to first token swings from 2s to 30s across providers), and mid-stream
    error events. OpenRouter emits one when a provider dies, and a hand-written
    parser reads that as a clean end, handing you half an article.

    Every SDK exception becomes a RuntimeError, which is what export catches to fall
    back to the original text."""
    import threading

    from openai import OpenAI

    cfg = load_config()
    client = OpenAI(api_key=key,
                    base_url=cfg.get("api_base") or "https://openrouter.ai/api/v1",
                    max_retries=3,               # default is 2
                    timeout=180.0)               # three minutes, then fail and retry
    got = False
    try:
        stream = client.chat.completions.create(
            model=cfg.get("model") or "deepseek/deepseek-v4-flash",
            messages=[{"role": "system", "content": TRANSLATE_PROMPT},
                      {"role": "user", "content": f"<article>\n{doc}\n</article>"}],
            stream=True)
        if stop is not None:                     # watchdog: closing the stream wakes the reader
            def watch():
                stop.wait()
                stream.close()                   # create() returned, so we hold the connection

            threading.Thread(target=watch, daemon=True).start()
        for event in stream:
            if not event.choices:                # trailing usage-only packet
                continue
            # content only; reasoning_content is the model thinking out loud
            if text := (event.choices[0].delta.content or ""):
                got = True
                yield text
    except Exception as e:                   # a mid-stream httpx error is not an OpenAIError
        if stop is not None and stop.is_set():
            return                               # we closed it ourselves, not an error
        raise RuntimeError(f"Translation failed: {e}") from e
    if not got and not (stop is not None and stop.is_set()):
        raise RuntimeError("Translation failed: the API returned nothing")


# -- cleanup for what the model will not stop doing --------------------

CODE_SPAN = re.compile(r"```.*?```|`[^`\n]*`|https?://\S+|<[^>\n]+>", re.S)


def zh_punct(md: str) -> str:
    """Half-width comma and semicolon to full-width on Chinese lines. The model will
    not stop emitting them; prompting for full-width and `--safe-mode` both failed.
    Code blocks, inline code, URLs, and HTML tags are skipped whole, and `1,000`
    keeps its comma. Colons stay: `> Source: <URL>` is found by its colon."""
    def fix(seg: str) -> str:
        return "\n".join(
            re.sub(r";", "；", re.sub(r"(?<![0-9]),(?![0-9])", "，", line))
            if re.search(r"[一-鿿]", line) else line       # pure-English lines untouched
            for line in seg.split("\n"))

    out, last = [], 0
    for m in CODE_SPAN.finditer(md):
        out += [fix(md[last:m.start()]), m[0]]
        last = m.end()
    return "".join(out + [fix(md[last:])])


def fix_fences(md: str) -> str:
    """An odd fence count means the model emitted a stray ```, and everything after
    it renders as code. Delete the one that opens a block but is followed by a blank
    line: a real code block does not start empty."""
    lines = md.split("\n")
    fences = [i for i, l in enumerate(lines) if l.lstrip().startswith("```")]
    if len(fences) % 2 == 0:
        return md
    for n, i in enumerate(fences):
        if n % 2 == 0 and (i + 1 >= len(lines) or not lines[i + 1].strip()):
            del lines[i]
            return "\n".join(lines)
    del lines[fences[-1]]                        # no candidate: drop the last one anyway
    return "\n".join(lines)


def translate(doc: str) -> str:
    """Whole-document translation, for export. Preview uses translate_stream."""
    return fix_fences(zh_punct("".join(translate_stream(doc))))


# -- terminal rendering ------------------------------------------------

def punct_lines(chunks):
    """Buffer stream fragments into whole lines before zh_punct: half a line cannot
    tell where a code span or URL ends. Lines inside a fence pass through."""
    buf, fence = "", False
    for chunk in chunks:
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if line.lstrip().startswith("```"):
                fence = not fence
                yield line + "\n"
            else:
                yield (line if fence else zh_punct(line)) + "\n"
    if buf:
        yield buf if fence else zh_punct(buf)


TICK, DRAIN, FLOOR = 0.03, 1.5, 60   # typing feel, shared by the CLI and the reader


async def stream_md(md, produce, stop=None) -> None:
    """produce() emits markdown fragments on a thread; this feeds them to the
    Markdown widget at a readable pace.

    MarkdownStream is Textual's widget for LLM output: append markdown, only the last
    block reflows, unclosed fences still render. One tick every 30ms, sized to drain
    the backlog within DRAIN seconds and never slower than FLOOR chars/sec, speeding
    up to 0.4s once the producer is done. Those three are the feel knobs.

    stop is cooperative, since Python threads cannot be killed: once set, the producer
    stops at its next fragment, so translation is not still burning tokens after you
    leave the page."""
    import asyncio
    import queue
    import threading

    from textual.widgets import Markdown

    stop = stop or threading.Event()
    q = queue.SimpleQueue()

    def run():
        try:
            for piece in produce():
                if stop.is_set():            # user is gone; stop fetching and translating
                    break
                q.put(piece)
        except Exception as e:               # catch everything: a dead producer posts no
            q.put(f"\n\n> ⚠️ {e}\n")     # sentinel and the renderer waits forever
        finally:
            q.put(None)

    def take():
        """Wait for a fragment with a timeout. The producer can sit on a 180s request,
        and asyncio joins to_thread threads on shutdown, so a blocking q.get would
        hang the whole process on exit."""
        try:
            return q.get(timeout=0.5)
        except queue.Empty:
            return ""                        # idle tick, then re-check stop

    threading.Thread(target=run, daemon=True).start()
    stream = Markdown.get_stream(md)
    buf, done = "", False
    try:
        while not stop.is_set():
            while not done and not stop.is_set() and (not buf or not q.empty()):  # drain what is queued
                if (item := await asyncio.to_thread(take)) is None:
                    done = True
                else:
                    buf += item
            if not buf:
                break
            n = max(1, round(max(FLOOR, len(buf) / (0.4 if done else DRAIN)) * TICK))
            await stream.write(buf[:n])
            buf = buf[n:]
            await asyncio.sleep(TICK)
    finally:
        await stream.stop()


def read_zh(doc: str) -> None:
    """Full-screen reader for `--read --zh`; the in-app t key uses
    reader.StreamScreen instead. Pace and stopping both live in stream_md."""
    import threading

    from textual import work
    from textual.app import App
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, Markdown

    stop = threading.Event()

    class Reader(App):
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self):
            with VerticalScroll():
                yield Markdown()
            yield Footer()

        def on_mount(self):
            self.query_one(VerticalScroll).anchor()   # follow the bottom, release on scroll up
            self.stream_it()

        def on_unmount(self):
            stop.set()                                # quitting stops the translation

        @work
        async def stream_it(self):
            await stream_md(self.query_one(Markdown),
                            lambda: punct_lines(translate_stream(doc, stop)), stop)

    Reader().run()


# -- writing files, entry point ----------------------------------------

def notify(title: str, body: str) -> None:
    """One macOS notification per finished background fetch. With translation on, an
    article takes a minute and the reader shows nothing in between.
    ponytail: one per article, so 30 selected means 30 notifications. Delete the
    success call below if that is too noisy and keep only the failures."""
    import json

    if not shutil.which("osascript"):        # macOS only; silently skip elsewhere
        return
    q = lambda t: json.dumps(t[:120], ensure_ascii=False)   # AppleScript escapes like JSON
    subprocess.run(["osascript", "-e",
                    f"display notification {q(body)} with title {q(title)}"],
                   capture_output=True)


def export_bg(items) -> list:
    """Hand [(url, title)] to background processes, one `grab.py --bg` each. Fetch,
    translate, write, and notify all happen there; the caller does not wait and can
    exit. Everything logs to grab.log, the only trace that path leaves.

    Returns a list matching items by index: None means started, otherwise the OSError
    for that one. By index rather than by value, because the same story on two feeds
    gives identical (url, title) and a retry would re-export the one that already
    worked. If the log file will not open, nothing starts and OSError propagates."""
    STATE.mkdir(parents=True, exist_ok=True)
    out = []
    with (STATE / "grab.log").open("a") as log:
        for url, title in items:
            try:
                subprocess.Popen([sys.executable, str(HERE), "--bg", url, title],
                                 stdout=log, stderr=log, start_new_session=True)
                out.append(None)
            except OSError as e:
                # grab.log is the only trace, so a failure to start needs a line too
                log.write(f"{time.strftime('%m-%d %H:%M:%S ')}✗ Could not start {title or url}: {e}\n")
                log.flush()
                out.append(e)
    return out


def grab(url: str, out_dir: Path, title: str | None = None, zh: bool = False) -> Path | None:
    check_url(url)                               # validate here too, before writing anything
    got = extract(url)
    if not got:
        return None
    page_title, body = got
    title = title or page_title       # a caller-supplied title matches what the list showed
    doc = document(title, url, body)
    if zh:
        try:
            doc = translate(doc)
            # The model reorders the header, typically moving the H1 below the source
            # line, so do not trust its layout: eat the leading run of H1 / source /
            # rule / blank, take the H1 as the filename, and rebuild with document().
            # Anchored at \A, so a --- separator inside the article survives.
            if head := re.match(r"(?:\s*(?:#\s+.+|>\s*Source:.*|-{3,})\s*\n)+", doc):
                if m := re.search(r"^#\s+(.+)$", head[0], re.M):
                    title = m[1].strip()
                    doc = document(title, url, doc[head.end():].strip())
        except RuntimeError as e:
            # The text is already in hand; a failed translation saves the original
            print(f"{stamp()}{e} — saving the original: {url}", file=sys.stderr)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = unique(out_dir / f"{slugify(title)}.md")
    path.write_text(doc, encoding="utf-8")
    return path


def main() -> None:
    args = sys.argv[1:]
    cfg = load_config()
    # Bare command in a terminal opens the reader; bare command in a pipe reads URLs
    # from stdin. Without the split, typing `rss` would block on stdin and look hung.
    if args[:1] == ["start"] or (not args and sys.stdin.isatty()):
        start(cfg)                              # execs, does not return
    # Preview: the same fetch, printed instead of saved. --zh adds translation.
    if args[:1] == ["--read"] and len(args) in (2, 3):
        zh = "--zh" in args
        url = next((a for a in args[1:] if not a.startswith("--")), "")
        try:
            check_url(url)
            got = extract(url)
        except Exception as e:                  # this feeds a pager; no traceback dump
            sys.exit(f"Fetch error: {e}")
        if not got:
            sys.exit(f"No article text found (anti-bot?) — open it manually: {url}")
        doc = document(got[0], url, got[1])
        if not zh:
            print(doc)
            return
        # An article takes tens of seconds, so waiting for all of it is a blank screen.
        # In a terminal open the Textual reader; in a pipe emit plain markdown.
        if sys.stdout.isatty():
            read_zh(doc)
            return
        out = 0
        try:
            for chunk in translate_stream(doc):
                print(chunk, end="", flush=True)
                out += 1
            print()
        except RuntimeError as e:
            if not out:                        # nothing came out: no original to fall back to
                sys.exit(str(e))
            print(f"\n\n⚠️ {e}")               # cut off midway: say so, keep what arrived
        except BrokenPipeError:                # downstream left early
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            sys.exit(0)
        except KeyboardInterrupt:
            sys.exit(130)
        return
    bg = args[:1] == ["--bg"]                   # our own background export: log and notify
    if bg:
        args = args[1:]
    if len(args) > 2:                           # extra arguments are a typo, not something to ignore
        sys.exit(f"Too many arguments ({len(args)}). Usage: grab.py [--bg] <URL> [title]")
    if args:
        jobs = [(args[0], args[1] if len(args) > 1 else None)]   # second argument is the title
    else:
        jobs = [(line.strip(), None) for line in sys.stdin if line.strip()]
    if not jobs:
        sys.exit(__doc__)

    out = out_dir(cfg)
    zh = bool(cfg.get("translate"))               # save the translation; source link is in the header
    failed = 0
    for url, title in jobs:
        name = title or url
        if bg:
            # Log the start before doing the work: translation takes a minute, and
            # logging only at the end leaves that minute indistinguishable from a
            # process that never started. flush, because stdout to a file is block-buffered.
            print(f"{stamp()}▶ {name}", flush=True)
        try:
            path = grab(url, out, title, zh)
        except Exception as e:                      # one failure must not end the batch
            print(f"{stamp()}✗ Fetch error {url}: {e}", file=sys.stderr, flush=True)
            if bg:
                notify("✗ Export failed", f"{name}\n{e}")
            failed += 1
            continue
        if path is None:
            print(f"{stamp()}✗ No article text (anti-bot?) — open it manually: {url}",
                  file=sys.stderr, flush=True)
            if bg:
                notify("✗ No article text", f"{name}\nAnti-bot, maybe — try opening it manually")
            failed += 1
        else:
            print(f"{stamp()}{'✓ ' if bg else ''}{path}", flush=True)   # goes to grab.log under --bg
            if bg:
                notify("✓ Exported", path.stem)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
