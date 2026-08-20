"""Recon: dump what PyFLP actually exposes from a real .flp.

Run: python tools/recon.py <file.flp> [more.flp ...]
"""
from __future__ import annotations

import sys
import traceback
from collections import Counter

import pyflp
from pyflp.channel import Instrument, Sampler
from pyflp.plugin import VSTPlugin


def dump(path: str) -> None:
    print("=" * 70)
    print(path)
    print("=" * 70)
    try:
        project = pyflp.parse(path)
    except Exception as exc:
        print(f"  !! parse failed: {type(exc).__name__}: {exc}")
        return

    for attr in ("title", "artists", "genre", "comments", "tempo", "ppq", "format", "version"):
        try:
            print(f"  {attr:10} = {getattr(project, attr)!r}")
        except Exception as exc:
            print(f"  {attr:10} !! {type(exc).__name__}: {exc}")

    # --- channels / sample paths ---
    kinds = Counter()
    samples, stock = [], []
    try:
        channels = list(project.channels)
    except Exception as exc:
        print(f"  !! channels failed: {type(exc).__name__}: {exc}")
        channels = []

    for ch in channels:
        kinds[type(ch).__name__] += 1
        if isinstance(ch, Sampler):
            try:
                sp = ch.sample_path
            except Exception as exc:
                print(f"  !! sample_path on iid={getattr(ch,'iid','?')}: {exc}")
                continue
            if sp is None or str(sp) in ("", "."):
                continue
            (stock if "%FLStudioFactoryData%" in str(sp) else samples).append((ch.display_name, str(sp)))

    print(f"  channel types: {dict(kinds)}  (total {len(channels)})")
    print(f"  user samples: {len(samples)}   factory samples: {len(stock)}")
    for name, sp in samples[:6]:
        print(f"     [user]    {name!r} -> {sp}")
    for name, sp in stock[:4]:
        print(f"     [factory] {name!r} -> {sp}")

    # --- plugins: channel instruments ---
    plugs = set()
    for ch in channels:
        try:
            iname = ch.internal_name
        except Exception:
            iname = None
        if isinstance(ch, Instrument):
            try:
                pl = ch.plugin
            except Exception as exc:
                print(f"  !! plugin read failed: {type(exc).__name__}: {exc}")
                pl = None
            if isinstance(pl, VSTPlugin):
                try:
                    plugs.add(f"VST:{pl.name} (vendor={pl.vendor})")
                except Exception as exc:
                    plugs.add(f"VST:<unreadable {exc}>")
            elif iname:
                plugs.add(f"native:{iname}")
        elif iname:
            plugs.add(f"native:{iname}")

    # --- plugins: mixer effect slots ---
    fx = set()
    try:
        for insert in project.mixer:
            for slot in insert:
                try:
                    iname = slot.internal_name
                except Exception:
                    continue
                if not iname:
                    continue
                if iname == "Fruity Wrapper":
                    pl = None
                    try:
                        pl = slot.plugin
                    except Exception:
                        pass
                    if isinstance(pl, VSTPlugin):
                        try:
                            fx.add(f"VST:{pl.name}")
                            continue
                        except Exception:
                            pass
                    fx.add(f"VST:{slot.name or '<unknown>'}")
                else:
                    fx.add(f"native:{iname}")
    except Exception as exc:
        print(f"  !! mixer walk failed: {type(exc).__name__}: {exc}")

    print(f"  instrument plugins ({len(plugs)}): {sorted(plugs)[:8]}")
    print(f"  mixer fx plugins  ({len(fx)}): {sorted(fx)[:8]}")

    # --- pattern notes (for key estimation) ---
    total_notes = 0
    pc = Counter()
    try:
        for pat in project.patterns:
            for note in pat.notes:
                total_notes += 1
                pc[note["key"] % 12] += 1
    except Exception as exc:
        print(f"  !! notes walk failed: {type(exc).__name__}: {exc}")
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    print(f"  notes: {total_notes}  pitch classes: "
          f"{[(names[k], v) for k, v in pc.most_common(5)]}")


if __name__ == "__main__":
    for f in sys.argv[1:]:
        try:
            dump(f)
        except Exception:
            traceback.print_exc()
