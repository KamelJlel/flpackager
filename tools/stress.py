"""End-to-end stress test: pack + unpack many real projects and validate.

For every project it checks:
  * the original .flp is byte-identical afterwards (the safety guarantee)
  * the packaged .flp differs from the original ONLY in SamplePath events
  * every bundled sample resolves relative to the packaged .flp
  * no two distinct sources collided onto one bundled filename
  * unpack reproduces a working folder
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import warnings
from collections import Counter

warnings.simplefilter("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyflp
from pyflp.channel import Sampler

from flpackager import core
from flpackager.flpwriter import iter_raw_events

SAMPLE_PATH_ID = 196


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(flp, workdir):
    problems = []
    before = md5(flp)

    analysis = core.analyze_project(flp)
    result = core.build_package(analysis, workdir, progress=None, make_zip=True)

    # 1. original untouched
    if md5(flp) != before:
        problems.append("ORIGINAL FILE WAS MODIFIED")

    packaged = os.path.join(result.bundle_dir, f"{os.path.splitext(os.path.basename(flp))[0]}.flp")
    if not os.path.isfile(packaged):
        problems.append("no packaged .flp produced")
        return problems, analysis

    # 2. only SamplePath events differ
    a = open(flp, "rb").read()
    b = open(packaged, "rb").read()
    ea, eb = list(iter_raw_events(a)), list(iter_raw_events(b))
    if len(ea) != len(eb):
        problems.append(f"event count changed {len(ea)} -> {len(eb)}")
    else:
        bad_ids = {
            x.id
            for x, y in zip(ea, eb)
            if a[x.start:x.end] != b[y.start:y.end] and x.id != SAMPLE_PATH_ID
        }
        if bad_ids:
            problems.append(f"non-SamplePath events changed: {sorted(bad_ids)}")

    # 3. bundled samples resolve next to the packaged .flp
    bundle = os.path.dirname(packaged)
    try:
        project = pyflp.parse(packaged)
        unresolved = 0
        for channel in project.channels:
            if not isinstance(channel, Sampler):
                continue
            path = channel.sample_path
            if path is None or str(path) in ("", "."):
                continue
            text = str(path)
            if text.startswith("samples\\") or text.startswith("samples/"):
                target = os.path.join(bundle, text.replace("\\", os.sep))
                if not os.path.isfile(target):
                    unresolved += 1
        if unresolved:
            problems.append(f"{unresolved} rewritten path(s) do not resolve")
    except Exception as exc:
        problems.append(f"packaged .flp failed to reparse: {exc}")

    # 4. no filename collisions between distinct sources
    pairs = {}
    for ref in analysis.unique_bundled:
        key = os.path.normcase(os.path.abspath(ref.resolved_path or ""))
        if ref.bundled_name in pairs and pairs[ref.bundled_name] != key:
            problems.append(f"collision on {ref.bundled_name}")
        pairs[ref.bundled_name] = key

    # 5. every bundled sample actually landed in samples/
    for ref in analysis.unique_bundled:
        if ref.note.startswith("copy failed"):
            continue
        if not os.path.isfile(os.path.join(bundle, "samples", ref.bundled_name)):
            problems.append(f"missing from bundle: {ref.bundled_name}")

    # 6. unpack round trip
    recv = os.path.join(workdir, "recv")
    unpacked = core.unpack_package(result.zip_path, recv)
    if not unpacked.flp_path or not os.path.isfile(unpacked.flp_path):
        problems.append("unpack produced no .flp")
    if unpacked.sample_count != len([r for r in analysis.unique_bundled
                                     if not r.note.startswith("copy failed")]):
        problems.append(
            f"unpack sample count {unpacked.sample_count} != "
            f"{len(analysis.unique_bundled)}"
        )

    return problems, analysis


def main():
    folder = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 40

    import glob
    files = sorted(glob.glob(os.path.join(folder, "*.flp")))

    # Prefer projects that actually exercise the interesting paths.
    scored = []
    for f in files:
        try:
            a = core.analyze_project(f)
        except Exception as exc:
            print(f"  ANALYZE FAIL {os.path.basename(f)}: {exc}")
            continue
        names = Counter(r.bundled_name for r in a.unique_bundled)
        interest = (
            len(a.builtin) * 3
            + sum(1 for r in a.samples if "renamed" in r.note) * 5
            + sum(1 for r in a.samples if r.note == core.DUPLICATE_NOTE) * 4
            + min(len(a.unique_bundled), 10)
            + min(len(a.missing), 5)
        )
        scored.append((interest, a.total_bundle_bytes, f))
    scored.sort(key=lambda t: (-t[0], t[1]))

    picked = [f for _, size, f in scored if size < 200 * 1024 * 1024][:limit]
    print(f"analyzed {len(scored)} projects, stress-testing {len(picked)}\n")

    failures = 0
    totals = Counter()
    for f in picked:
        workdir = tempfile.mkdtemp(prefix="flpack_")
        try:
            problems, a = check(f, workdir)
            totals["bundled"] += len(a.unique_bundled)
            totals["missing"] += len(a.missing)
            totals["builtin"] += len(a.builtin)
            totals["renamed"] += sum(1 for r in a.samples if "renamed" in r.note)
            totals["dupes"] += sum(1 for r in a.samples if r.note == core.DUPLICATE_NOTE)
            if problems:
                failures += 1
                print(f"FAIL {os.path.basename(f)}")
                for p in problems:
                    print(f"       - {p}")
            else:
                print(
                    f"ok   {os.path.basename(f):40} "
                    f"bundled={len(a.unique_bundled):3} missing={len(a.missing):3} "
                    f"builtin={len(a.builtin):2} renamed={sum(1 for r in a.samples if 'renamed' in r.note)}"
                )
        except Exception as exc:
            failures += 1
            print(f"ERROR {os.path.basename(f)}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc(limit=4)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    print()
    print(f"projects tested : {len(picked)}")
    print(f"failures        : {failures}")
    print(f"totals          : {dict(totals)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
