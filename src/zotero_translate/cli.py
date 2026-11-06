"""Command line: ``zotero-translate serve`` and ``zotero-translate run``.

``serve`` starts the translation service. ``run`` runs one paper task on a
PDF directly, without Zotero or the service, and writes ``mono.pdf``,
``dual.pdf``, ``dossier.json``, ``terms.json``, ``report.json`` and
``requests.jsonl`` (the request records) into the output directory
(``--user-profile-rules``, for development: with the prompt rules of a user
model profile's task, M4 ticket 15); as it
exits, ``report.json``'s ``requests`` is brought up to date with
``requests.jsonl``. Both share the
model response cache in the data directory.

``serve`` exits with 2 when an environment file is missing or incomplete
and 1 when another service holds the data directory or the port is taken.
``run`` exits with 0 when the paper task succeeds, 1 when it fails (the
task report's ``state`` is ``failed`` and its ``error`` says why) and 2 when
an environment file is missing or incomplete. It ends the process itself
(``zotero_translate.process_exit``) once the paper task is over and
``report.json`` is up to date, skipping native libraries' exit handlers,
which sometimes hang (``main`` does not return for ``run``). A script that
logs the exit status must read ``$?`` before anything else runs: in
``echo "$(date) exit=$?"`` the command substitution resets it to 0.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import shutil
import sys
import time
from pathlib import Path

from zotero_translate import settings as s
from zotero_translate.process_exit import run_then_exit


def main(argv: list[str] | None = None) -> int:
    """Run the command; ``serve`` returns its exit code, ``run`` ends the process itself."""
    parser = argparse.ArgumentParser(prog="zotero-translate")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="start the translation service on 127.0.0.1")
    serve.add_argument("--port", type=int, default=s.DEFAULT_PORT, help="0 picks a free port")
    _add_model_options(serve)
    serve.add_argument(
        "--profiles-dir",
        type=Path,
        default=s.DEFAULT_PROFILES_DIR,
        help="the user model profiles' files, <id>.env (ADR 0005); "
        "default ~/.config/zotero-translate/profiles or ZT_PROFILES_DIR",
    )

    run = commands.add_parser("run", help="run one paper task on a PDF")
    run.add_argument("pdf", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    _add_model_options(run)
    run.add_argument(
        "--user-profile-rules",
        action="store_true",
        help="for development: the prompt rules a user model profile's task has "
        "(M4 ticket 15); the built-in Gemini has none",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        from zotero_translate.service import serve as serve_forever

        try:  # fail at startup, not on the first task
            s.load_pipeline_settings(args.env_file, args.argon_env_file, args.concurrency)
        except s.SettingsError as e:
            print(e, file=sys.stderr)
            return 2
        return serve_forever(
            args.data_dir,
            args.env_file,
            args.argon_env_file,
            args.port,
            args.concurrency,
            hedging=not args.no_hedging,
            profiles_dir=args.profiles_dir,
        )
    run_then_exit(lambda: _run(args))  # does not return


def _add_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        type=Path,
        default=s.DEFAULT_ENV_FILE,
        help="flash: file with OPENAI_BASE_URL, OPENAI_API_KEY and AI_DEFAULT_MODEL",
    )
    parser.add_argument(
        "--argon-env-file",
        type=Path,
        default=s.DEFAULT_ARGON_ENV_FILE,
        help="argon (paper dossier, term list, timed-out requests): the same keys, "
        "and ZT_CONCURRENCY",
    )
    parser.add_argument("--concurrency", type=int, default=s.DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--no-hedging",
        action="store_true",
        help="no argon copy of a slow flash translation batch or review; flash requests "
        "go to argon after 2 timeouts in a row instead (ADR 0004)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=s.DEFAULT_DATA_DIR,
        help="tasks and the model response cache (model-cache/)",
    )


def _run(args) -> int:
    from zotero_translate.paper_task import PaperTaskError
    from zotero_translate.paper_task import run_paper_task
    from zotero_translate.request_log import update_report

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        model_settings = s.load_pipeline_settings(
            args.env_file,
            args.argon_env_file,
            args.concurrency,
            s.model_cache_dir(args.data_dir),
            hedging=not args.no_hedging,
            user_profile_rules=args.user_profile_rules,
        )
    except s.SettingsError as e:
        print(e, file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source = args.output_dir / "source.pdf"
    if source.resolve() != args.pdf.resolve():
        shutil.copyfile(args.pdf, source)
    started = time.monotonic()
    # A failed paper task does not wait for its algorithm requests, so sends
    # can end after the report is written. The exit (``run_then_exit``)
    # joins those threads before it runs the atexit handlers: then the
    # report's request records are made whole from requests.jsonl.
    atexit.register(update_report, args.output_dir / "report.json")
    try:
        result = run_paper_task(
            source,
            args.output_dir,
            model_settings,
            on_progress=lambda stage, progress: print(f"[{progress:3d}%] {stage}", flush=True),
        )
    except PaperTaskError as e:
        print(f"论文任务失败：{e}；任务报告 {args.output_dir / 'report.json'}", file=sys.stderr)
        return 1
    print(
        f"完成，用时 {time.monotonic() - started:.1f} 秒；"
        f"模型请求 {result.model_requests} 次，最大在途 flash {result.max_in_flight}、"
        f"argon {result.argon_max_in_flight}；转发 argon {result.forwarded_requests} 次；"
        f"保留原文 {result.kept_source_paragraphs} 段、{result.kept_source_algorithms} 个算法；"
        f"单语译文 {result.mono_pdf}；双语对照 {result.dual_pdf}；"
        f"任务报告 {args.output_dir / 'report.json'}",
        flush=True,
    )
    return 0
