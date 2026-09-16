import json, gzip, io, re, urllib.request, concurrent.futures, collections, sys

d = json.load(open('cfh.json'))
BASE = "https://files.wwpdb.org/pub"

flr_cat = re.compile(rb"^\s*(_flr_[A-Za-z_0-9]+)\.", re.M)
dtline = re.compile(rb"FRET|[Ff]luorescen|F.rster", re.M)


def one(item):
    eid, info = item
    path = info["mmcif"][0]
    url = BASE + path
    try:
        raw = urllib.request.urlopen(url, timeout=180).read()
        txt = gzip.decompress(raw)
    except Exception as e:
        return eid, None, str(e)[:80], 0
    cats = sorted(set(m.decode() for m in flr_cat.findall(txt)))
    # extract ihm_dataset_list data types
    dts = set()
    for line in txt.splitlines():
        s = line.strip()
        if b"FRET" in s or b"luorescen" in s:
            dts.add(s[:120].decode(errors="replace"))
    return eid, cats, sorted(dts)[:6], len(txt)


res = {}
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
    for i, (eid, cats, dts, n) in enumerate(ex.map(one, d.items())):
        res[eid] = (cats, dts, n)
        if i % 50 == 0:
            print("...", i, file=sys.stderr)

json.dump({k: v for k, v in res.items()}, open("scan_out.json", "w"))

with_flr = {k: v for k, v in res.items() if v[0]}
print("total entries scanned:", len(res))
print("entries with _flr_ categories:", len(with_flr))
print(sorted(with_flr))
c = collections.Counter()
for k, (cats, dts, n) in with_flr.items():
    c.update(cats)
print("\ncategory usage across entries:")
for cat, n in c.most_common():
    print(f"  {cat}: {n}")

print("\nentries mentioning FRET/fluorescence but WITHOUT _flr_:")
mention = {k: v for k, v in res.items() if not v[0] and v[1]}
print(len(mention))
for k, v in sorted(mention.items()):
    print(" ", k, v[1][:3])
