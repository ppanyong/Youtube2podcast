# Youtube2podcast

把带英文字幕的 YouTube 长视频做成一条中文音频，并放上一份 Markdown 笔记。本地网页里显示为「成长书房」。

没有英文字幕的视频会跳过。每条视频只生成一个 MP3。

## 安装

需要 Python 3.12 和本机的 `ffmpeg`。

```bash
# macOS 若还没有 ffmpeg
brew install ffmpeg

git clone https://github.com/ppanyong/Youtube2podcast.git
cd Youtube2podcast
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
youtube2podcast serve
```

浏览器打开 `http://127.0.0.1:8765`。第一次使用先点右上角齿轮，填大模型和语音接口；密钥会写进本机的 `.env`。

Windows 上把 `source .venv/bin/activate` 换成 `.venv\Scripts\activate`。

## 密钥不会进仓库

`.env`、任务数据库 `data/` 已写入 `.gitignore`。仓库里只有 `.env.example`，里面是占位符。发布或推送前可以确认：

```bash
git status
```

列表里不应出现 `.env` 或 `data/`。

## 成品

```
~/Learning/机器学习/2026-09-25 注意力机制入门/
  注意力机制入门.mp3
  summary.md
  meta.json
```

专辑名默认用视频标题。同一条视频如果已经成功产出，再次提交会跳过。

任务纸条上可以预听 MP3，也可以打开原视频。失败或已停止的任务可以继续；进行中的任务可以停止。

## 命令行

```bash
youtube2podcast run "https://www.youtube.com/watch?v=..." --output ~/Learning
youtube2podcast run --list urls.txt --output ~/Learning
```

`--album` 可以指定专辑目录名；留空时使用视频标题。
