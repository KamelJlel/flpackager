"""Parse -> save -> byte-compare. Detects lossy round-trips."""
import glob, os, sys, io, warnings, hashlib
warnings.simplefilter("ignore")
import pyflp

out = os.path.join(os.environ.get("TMP", "."), "_rt_test.flp")
same = diff = err = 0
bad = []
files = sorted(glob.glob(os.path.join(sys.argv[1], "*.flp")))
for f in files:
    try:
        p = pyflp.parse(f)
        pyflp.save(p, out)
    except Exception as exc:
        err += 1; bad.append((os.path.basename(f), f"ERR {type(exc).__name__}: {exc}")); continue
    a = open(f, "rb").read(); b = open(out, "rb").read()
    if a == b:
        same += 1
    else:
        diff += 1
        bad.append((os.path.basename(f), f"DIFF orig={len(a)} new={len(b)} delta={len(b)-len(a)}"))
print(f"files={len(files)} identical={same} differing={diff} errors={err}")
for n, why in bad[:15]:
    print("  ", n, "->", why)
