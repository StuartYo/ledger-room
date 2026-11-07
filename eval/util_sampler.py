"""GPU busy sampler for the Arc Pro B70 host, run beside eval.bench_concurrency.

On the B70 host `xpu-smi dump` returns N/A for every engine-utilisation metric (0, 22, 9, 17); only
utilization.memory (5) has a value. The Xe kernel driver does expose per-client engine cycles in
/proc/<pid>/fdinfo, so this reads those instead:

    busy(compute) = sum over this user's clients of d(drm-cycles-ccs) / (d(drm-total-cycles-ccs) * capacity)

sampled every 5 s per card (drm-pdev). "Busy" means the compute engine had work queued for that share of
the time; it is not a measure of how many execution units were active.

Host use (minutes = how long to sample; start it right after the long run):
    setsid nohup python3 eval/util_sampler.py 72 ~/util_longrun.csv < /dev/null > ~/util_sampler.log 2>&1 &
Summary: python3 eval/util_sampler.py --summary ~/util_longrun.csv
"""
import glob
import json
import os
import statistics
import sys
import time


def snap():
    s = {}
    me = os.getuid()
    for fd in glob.glob("/proc/[0-9]*/fdinfo/*"):
        try:
            if os.stat(fd.split("/fdinfo")[0]).st_uid != me:
                continue
            txt = open(fd).read()
        except Exception:
            continue
        if "xe" not in txt:
            continue
        kv = {}
        for line in txt.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
        if kv.get("drm-driver") != "xe":
            continue
        p = kv.get("drm-pdev")
        cid = kv.get("drm-client-id", fd)
        d = s.setdefault(p, {"clients": {}, "tot": 0, "cap": int(kv.get("drm-engine-capacity-ccs", 1))})
        d["clients"][cid] = (int(kv.get("drm-cycles-ccs", 0)), int(kv.get("drm-cycles-rcs", 0)))
        d["tot"] = max(d["tot"], int(kv.get("drm-total-cycles-ccs", 0)))
    return s


def sample(minutes, path):
    out = open(os.path.expanduser(path), "w")
    out.write("t,pdev,busy_ccs,busy_rcs,n_fds\n")
    end = time.time() + minutes * 60
    prev = snap()
    while time.time() < end:
        time.sleep(5)
        cur = snap()
        for p, d in cur.items():
            if p not in prev:
                continue
            dt = d["tot"] - prev[p]["tot"]
            if dt <= 0:
                continue
            old = prev[p]["clients"]
            cc = sum(v[0] - old.get(c, v)[0] for c, v in d["clients"].items())
            rc = sum(v[1] - old.get(c, v)[1] for c, v in d["clients"].items())
            out.write(f"{time.time():.0f},{p},{cc / (dt * d['cap']):.4f},{rc / dt:.4f},{len(d['clients'])}\n")
            out.flush()
        prev = cur


def summary(path):
    rows = {}
    for line in open(os.path.expanduser(path)).read().splitlines()[1:]:
        t, p, b, _, _ = line.split(",")
        rows.setdefault(p, []).append((int(t), float(b)))
    res = {}
    for p, v in rows.items():
        # a reader restart can reuse a drm-client-id with its counter back at 0: that 5 s delta is
        # negative and meaningless, so it is dropped (and counted); zeros (model reload gaps) are kept
        b = sorted(min(x[1], 1.0) for x in v if x[1] >= 0)
        res[p] = {
            "samples": len(b),
            "dropped_negative_at_restart": sum(x[1] < 0 for x in v),
            "samples_at_zero": sum(x == 0 for x in b),
            "minutes": round((v[-1][0] - v[0][0]) / 60, 1),
            "mean_busy": round(statistics.mean(b), 3),
            "p50_busy": round(b[len(b) // 2], 3),
            "p05_busy": round(b[int(len(b) * 0.05)], 3),
            "share_of_samples_at_or_above_0.90": round(sum(x >= 0.9 for x in b) / len(b), 3),
        }
    return res


if __name__ == "__main__":
    if sys.argv[1] == "--summary":
        print(json.dumps(summary(sys.argv[2]), indent=1))
    else:
        sample(float(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "~/util_longrun.csv")
