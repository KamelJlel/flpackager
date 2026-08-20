"""Command line interface for flpackager.

A thin presentation layer over :mod:`flpackager.core` -- all of the real work
(and all of the structured data) lives there so a GUI can reuse it unchanged.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from . import core


# ---------------------------------------------------------------------------
# Small terminal helpers
# ---------------------------------------------------------------------------


def _supports_progress() -> bool:
    return sys.stderr.isatty()


class _Progress:
    """One-line, overwriting progress reporter. Silent when piped to a file."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and _supports_progress()
        self._width = 0

    def __call__(self, done: int, total: int, label: str) -> None:
        if not self.enabled or not total:
            return
        percent = done * 100 // total
        line = f"  [{done}/{total}] {percent:3d}%  {label}"
        line = line[:110]
        pad = max(0, self._width - len(line))
        self._width = len(line)
        sys.stderr.write("\r" + line + " " * pad)
        sys.stderr.flush()

    def done(self) -> None:
        if self.enabled and self._width:
            sys.stderr.write("\r" + " " * self._width + "\r")
            sys.stderr.flush()
            self._width = 0


def _human_bytes(size: Optional[int]) -> str:
    return core._human_bytes(size)


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


def _print_analysis_summary(analysis: core.ProjectAnalysis) -> None:
    print(f"Project      : {analysis.project_name}")
    print(f"Tempo        : {analysis.tempo if analysis.tempo is not None else 'unknown'} BPM")
    if analysis.key:
        print(
            f"Key          : {analysis.key.key}  (estimated, "
            f"confidence {analysis.key.confidence:.2f})"
        )
    else:
        print("Key          : not estimated (no clear tonal content)")
    if analysis.fl_version:
        print(f"FL Studio    : {analysis.fl_version}")

    print()
    print(f"Plugins required ({len(analysis.plugins)}):")
    if analysis.plugins:
        for plugin in analysis.plugins:
            vendor = f" [{plugin.vendor}]" if plugin.vendor else ""
            print(f"  - {plugin.name}{vendor}  ({plugin.kind})")
    else:
        print("  (none detected)")

    unique = analysis.unique_bundled
    print()
    print(f"Samples to bundle ({len(unique)}, {_human_bytes(analysis.total_bundle_bytes)}):")
    for ref in unique:
        note = f"   <- {ref.note}" if ref.note else ""
        print(f"  + {ref.bundled_name}{note}")
    if not unique:
        print("  (none)")

    if analysis.builtin:
        print()
        print(f"Built-in / factory samples ({len(analysis.builtin)}) - no copy needed:")
        for ref in analysis.builtin:
            print(f"  = {ref.original_path}")

    if analysis.missing:
        print()
        print(f"MISSING samples ({len(analysis.missing)}) - will be listed in manifest.txt:")
        for ref in analysis.missing:
            who = f'  (channel "{ref.channel_name}")' if ref.channel_name else ""
            print(f"  ! {ref.original_path}{who}")

    if analysis.warnings:
        print()
        print(f"Warnings ({len(analysis.warnings)}):")
        for warning in analysis.warnings:
            print(f"  * {warning}")


def cmd_pack(args: argparse.Namespace) -> int:
    flp_path = args.flp
    if not os.path.isfile(flp_path):
        print(f"error: no such file: {flp_path}", file=sys.stderr)
        return 2

    print(f"Reading {flp_path} ...")
    try:
        analysis = core.analyze_project(flp_path)
    except Exception as exc:
        print(f"error: couldn't read the project: {exc}", file=sys.stderr)
        return 1

    print()
    _print_analysis_summary(analysis)
    print()

    if args.dry_run:
        print("-- dry run: nothing was written --")
        target = os.path.abspath(args.output or ".")
        bundle = os.path.join(target, f"{os.path.splitext(os.path.basename(flp_path))[0]}_package")
        print(f"Would create : {bundle}")
        print(f"Would create : {bundle}.zip")
        return 0

    output_dir = args.output or "."
    os.makedirs(output_dir, exist_ok=True)

    progress = _Progress(enabled=not args.quiet)
    try:
        result = core.build_package(
            analysis, output_dir, progress=progress, make_zip=not args.no_zip
        )
    except Exception as exc:
        progress.done()
        print(f"error: packaging failed: {exc}", file=sys.stderr)
        return 1
    progress.done()

    print(f"Copied {result.copied_files} sample(s), {_human_bytes(result.copied_bytes)}")
    print(f"Bundle : {result.bundle_dir}")
    if result.zip_path:
        size = os.path.getsize(result.zip_path)
        print(f"Package: {result.zip_path}  ({_human_bytes(size)})")

    for warning in result.warnings:
        print(f"  * {warning}")

    if analysis.missing:
        print()
        print(
            f"note: {len(analysis.missing)} sample(s) could not be found and are "
            "not in the package. See manifest.txt."
        )
    return 0


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------


def cmd_unpack(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.package):
        print(f"error: no such file: {args.package}", file=sys.stderr)
        return 2

    output_dir = args.output or "."
    os.makedirs(output_dir, exist_ok=True)

    progress = _Progress(enabled=not args.quiet)
    try:
        result = core.unpack_package(args.package, output_dir, progress=progress)
    except Exception as exc:
        progress.done()
        print(f"error: couldn't unpack: {exc}", file=sys.stderr)
        return 1
    progress.done()

    print(f"Unpacked to : {result.output_dir}")
    if result.flp_path:
        print(f"Project file: {result.flp_path}")
    print(f"Samples     : {result.sample_count}")
    print()

    if result.manifest_text:
        print(result.manifest_text.rstrip())
    else:
        print("(no manifest.txt in this package)")

    for warning in result.warnings:
        print(f"  * {warning}")

    print()
    if result.flp_path:
        print("Open the .flp above in FL Studio -- it points at ./samples/ next to it.")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flpackager",
        description=(
            "Package an FL Studio project (.flp) together with its samples so it "
            "opens cleanly on someone else's machine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"flpackager {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack_parser = subparsers.add_parser(
        "pack", help="bundle a project and its samples into a zip"
    )
    pack_parser.add_argument("flp", help="path to the .flp project file")
    pack_parser.add_argument(
        "-o", "--output", default=".", help="output directory (default: current directory)"
    )
    pack_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be bundled without writing anything",
    )
    pack_parser.add_argument(
        "--no-zip", action="store_true", help="build the bundle folder but skip the zip"
    )
    pack_parser.add_argument("-q", "--quiet", action="store_true", help="hide the progress line")
    pack_parser.set_defaults(func=cmd_pack)

    unpack_parser = subparsers.add_parser(
        "unpack", help="extract a package and print its manifest"
    )
    unpack_parser.add_argument("package", help="path to the *_package.zip")
    unpack_parser.add_argument(
        "-o", "--output", default=".", help="output directory (default: current directory)"
    )
    unpack_parser.add_argument("-q", "--quiet", action="store_true", help="hide the progress line")
    unpack_parser.set_defaults(func=cmd_unpack)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
