"""Identify which event IDs do not round-trip byte-exactly."""
import glob, io, os, struct, sys, warnings
from collections import Counter
warnings.simplefilter("ignore")
import construct as c
import pyflp
from pyflp._events import WORD, DWORD, TEXT

def raw_events(path):
    """Re-walk the file exactly like pyflp.parse, yielding (id, full_raw_chunk)."""
    data = open(path, "rb").read()
    s = io.BytesIO(data)
    s.seek(22)
    end = len(data)
    while s.tell() < end:
        start = s.tell()
        i = int.from_bytes(s.read(1), "little")
        if i < WORD: s.read(1)
        elif i < DWORD: s.read(2)
        elif i < TEXT: s.read(4)
        else:
            n = c.VarInt.parse_stream(s); s.read(n)
        yield i, data[start:s.tell()]

bad = Counter()
examples = {}
for f in sorted(glob.glob(os.path.join(sys.argv[1], "*.flp"))):
    try:
        p = pyflp.parse(f)
        raws = list(raw_events(f))
        evs = list(p.events)
    except Exception:
        continue
    if len(raws) != len(evs):
        bad[f"COUNT_MISMATCH raw={len(raws)} ev={len(evs)}"] += 1
        continue
    for (rid, rb), ev in zip(raws, evs):
        try: nb = bytes(ev)
        except Exception as exc:
            bad[f"id={rid} BUILD_FAIL {type(exc).__name__}"] += 1; continue
        if nb != rb:
            k = f"id={rid} ({type(ev).__name__}) dlen={len(nb)-len(rb)}"
            bad[k] += 1
            examples.setdefault(k, (os.path.basename(f), rb[:48].hex(), nb[:48].hex()))
for k, v in bad.most_common(20):
    print(f"{v:5}  {k}")
    if k in examples:
        n, a, b = examples[k]
        print(f"        file={n}\n        orig={a}\n        new ={b}")
