"""Sweep every .flp in a folder, count what breaks. Robustness recon."""
from __future__ import annotations
import glob, sys, os
from collections import Counter
import pyflp
from pyflp.channel import Instrument, Sampler
from pyflp.plugin import VSTPlugin

fails, ok = Counter(), 0
missing, present, factory = 0, 0, 0
weird_paths = set()
inst_kinds = Counter()

for f in sorted(glob.glob(os.path.join(sys.argv[1], "*.flp"))):
    try:
        p = pyflp.parse(f)
    except Exception as exc:
        fails[f"parse:{type(exc).__name__}"] += 1
        continue
    ok += 1
    try:
        chans = list(p.channels)
    except Exception as exc:
        fails[f"channels:{type(exc).__name__}"] += 1
        continue
    for ch in chans:
        if isinstance(ch, Sampler):
            try:
                sp = ch.sample_path
            except Exception as exc:
                fails[f"sample_path:{type(exc).__name__}"] += 1
                continue
            if sp is None:
                continue
            s = str(sp)
            if s in ("", "."):
                continue
            if "%FLStudioFactoryData%" in s:
                factory += 1
            elif not (len(s) > 1 and s[1] == ":") and not s.startswith("\\\\"):
                weird_paths.add(s)      # non-absolute / unusual
            elif os.path.exists(s):
                present += 1
            else:
                missing += 1
        if isinstance(ch, Instrument):
            try:
                pl = ch.plugin
                inst_kinds[type(pl).__name__] += 1
                if isinstance(pl, VSTPlugin):
                    _ = pl.name, pl.vendor
            except Exception as exc:
                fails[f"plugin:{type(exc).__name__}"] += 1
    try:
        for pat in p.patterns:
            for n in pat.notes:
                _ = n["key"]
    except Exception as exc:
        fails[f"notes:{type(exc).__name__}"] += 1
    try:
        for ins in p.mixer:
            for slot in ins:
                _ = slot.internal_name
    except Exception as exc:
        fails[f"mixer:{type(exc).__name__}"] += 1

print(f"parsed ok: {ok}")
print(f"samples -> present {present} | missing {missing} | factory {factory}")
print(f"instrument plugin kinds: {dict(inst_kinds)}")
print("FAILURES:")
for k, v in fails.most_common():
    print(f"   {v:4}  {k}")
print("unusual paths (first 10):", list(weird_paths)[:10])
