from __future__ import annotations

import argparse
from pathlib import Path

from youtube2podcast.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="youtube2podcast", description="把 YouTube 长视频转成中文学习音频")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="打开本机网页，提交任务并查看进度")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    run = sub.add_parser("run", help="直接转换一条或多条视频")
    run.add_argument("urls", nargs="*", help="YouTube 链接")
    run.add_argument("--output", required=True, help="成品根目录")
    run.add_argument("--album", default="", help="专辑名，留空则使用视频标题")
    run.add_argument("--list", dest="list_path", help="每行一个链接的文本文件")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.cmd == "serve":
        import uvicorn

        from youtube2podcast.config import load_env
        from youtube2podcast.runtime import AppRuntime
        from youtube2podcast.server import JobRunner, create_app

        env_path = Path.cwd() / ".env"
        load_env(env_path)
        runtime = AppRuntime(settings, env_path)
        runner = JobRunner(runtime.store, runtime.run_task)
        app = create_app(runtime.store, runner, settings_store=runtime.settings_store)
        host = args.host or settings.host
        port = args.port or settings.port
        uvicorn.run(app, host=host, port=port)
        return 0

    urls = [item.strip() for item in args.urls if item.strip()]
    if args.list_path:
        urls.extend(
            line.strip()
            for line in Path(args.list_path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    if not urls:
        parser.error("请提供视频链接，或使用 --list")

    from youtube2podcast.runtime import build_runtime

    store, pipeline = build_runtime(settings)
    for url in urls:
        task = store.create(url, args.album, args.output)
        pipeline.run(task["id"])
        saved = store.get(task["id"])
        print(f"[{saved['status']}] {saved.get('title') or url} {saved.get('detail') or ''}")
        if saved.get("output_path"):
            print(saved["output_path"])
        if saved.get("error"):
            print(saved["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
