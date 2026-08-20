"""Identity check: rewrite with no replacements must be byte-identical."""
import glob, os, sys, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flpackager.flpwriter import rewrite_sample_paths, iter_raw_events, FLPStructureError

same = diff = err = 0
files = sorted(glob.glob(os.path.join(sys.argv[1], "*.flp")))
for f in files:
    data = open(f, "rb").read()
    try:
        out = rewrite_sample_paths(data, {}, unicode=True)
    except FLPStructureError as exc:
        err += 1; print("  ERR", os.path.basename(f), exc); continue
    if out == data:
        same += 1
    else:
        diff += 1
        print("  DIFF", os.path.basename(f), len(data), len(out))
print(f"files={len(files)} identical={same} differing={diff} errors={err}")

# also confirm raw event count matches pyflp's event count (index alignment)
import pyflp
mism = 0
for f in files:
    try:
        p = pyflp.parse(f)
        if len(list(iter_raw_events(open(f,'rb').read()))) != len(list(p.events)):
            mism += 1; print("  COUNT MISMATCH", os.path.basename(f))
    except Exception as exc:
        print("  parse err", os.path.basename(f), exc)
print(f"event-count mismatches: {mism}")
