"""How many missing samples could we recover by searching the user's folders?"""
import glob, os, sys, warnings
from collections import Counter, defaultdict
warnings.simplefilter("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flpackager import core

AUDIO = core.AUDIO_SUFFIXES
roots = [os.path.expanduser("~/Music"), os.path.expanduser("~/Downloads"),
         os.path.expanduser("~/Documents"), os.path.expanduser("~/Desktop")]

index = defaultdict(list)
files = 0
for root in roots:
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in AUDIO:
                index[fn.lower()].append(os.path.join(dirpath, fn))
                files += 1
print(f"indexed {files} audio files under Music/Downloads/Documents/Desktop")
print(f"distinct filenames: {len(index)}")
print()

miss = recov = ambig = 0
proj_before = proj_after = 0
for f in sorted(glob.glob(r"C:\Users\TRIEDENT\Desktop\Main\beats\*.flp")):
    try: a = core.analyze_project(f)
    except Exception: continue
    m = a.missing
    if not m: 
        if a.samples: proj_before += 1; proj_after += 1
        continue
    hits = 0
    for ref in m:
        miss += 1
        base = os.path.basename(ref.original_path.replace("\\", "/")).lower()
        cands = index.get(base, [])
        if len(cands) == 1: recov += 1; hits += 1
        elif len(cands) > 1: ambig += 1; hits += 1
    if hits == len(m): proj_after += 1
print(f"missing references      : {miss}")
print(f"  recoverable (1 match) : {recov}  ({recov*100//max(miss,1)}%)")
print(f"  ambiguous (>1 match)  : {ambig}  ({ambig*100//max(miss,1)}%)")
print(f"  unrecoverable         : {miss-recov-ambig}")
print()
print(f"projects fully complete now        : {proj_before}")
print(f"projects fully complete after fix  : {proj_after}")
