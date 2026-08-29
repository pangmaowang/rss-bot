# rss

[English](README.md) · [中文](README.zh-CN.md)

一个按我自己口味做的终端 RSS 阅读器。界面收拾得干净，功能克制，一共两个 Python 文件。

![源列表](docs/shot-feeds.svg)

![文章列表](docs/shot-articles.svg)

![阅读页](docs/shot-reading.svg)

> 界面文字是英文，代码注释是中文。

## 能干什么

- **订阅源写在一个配置文件里。** 地址、显示名、tag。打了 tag 配上 `rotate`，每天只显示轮到的那组。
- **在终端里直接读。** 随便在哪敲 `rss`。不用开浏览器，没有后台进程，不需要同步账号。
- **抓得到真正的正文。** RSS 摘要基本等于没有，所以按 `o` 回原站把整页抓下来，就地渲染。
- **AI 翻译。** 按 `t` 边翻边出字，2 到 4 秒见第一句。
- **十一个键，没有配置 DSL。**
- **Textual 做的界面。** 流式 markdown、表格、代码块、语法高亮。
- **导出成 Markdown。** 选一批按 `b`，干净的 `.md` 落进你的笔记目录。

## 装

```bash
git clone https://github.com/pangmaowang/rss-bot && cd rss-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
chmod +x grab.py reader.py
ln -sf "$PWD/grab.py" /opt/homebrew/bin/rss
```

订阅源和导出目录改 `config.toml`。翻译要一个 API key，放在 `.env` 里：

```bash
cp .env.example .env && chmod 600 .env && vi .env
```

任何 OpenAI 兼容的 `/chat/completions` 端点都行，换 OpenRouter、DeepSeek 或者本地 vLLM 只改一行。

## 快捷键

| 键 | 干什么 |
|---|---|
| 回车 | 源里带的那段 RSS 摘要 |
| `→` / `←` | 进下一级 / 回上一级 |
| `o` | 回原站抓全文，就地渲染 |
| `t` | 抓全文，再流式翻成中文 |
| `w` | 丢给系统浏览器 |
| `/` | 按标题过滤——空格分词、全部命中才显示；回车保留过滤，Esc 清掉 |
| 空格 | 选中或取消，光标自动下移 |
| `a` / `u` | 全选当前源 / 清空选择 |
| `b` | 后台导出选中的那些 |
| `r` | 刷新全部源 |
| `q` | 退一层，在源列表里是退出 |

选中状态跨源累计。已读状态存在 `~/.local/state/grab/cache.db`。

## 许可

MIT，见 [LICENSE](LICENSE)。sqlite schema、guid 回退顺序和 `feeds` 的行语法沿用 [newsboat](https://github.com/newsboat/newsboat)（MIT），这样老库还能接着用。仓库里没有 newsboat 的代码。
