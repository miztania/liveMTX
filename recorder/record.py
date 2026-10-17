"""STRYK live recorder.

ดึงสถานะไลฟ์ + จำนวนคนดูของทุกช่องใน channels.json ทุก INTERVAL วินาที
แล้วเขียนลง live.json (และ push ขึ้น branch `data` ถ้าตั้ง GIT_PUSH=1)

Quota ต่อรอบ: playlistItems 1 unit ต่อช่อง + videos.list 1 unit  (~10 units/นาที)
"""
import json, os, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

KEY = os.environ.get("YT_API_KEY", "")
API = os.environ.get("API_BASE", "https://www.googleapis.com/youtube/v3/")
CFG = os.environ.get("CHANNELS", "channels.json")
OUT = os.environ.get("OUT", "live.json")
INTERVAL = int(os.environ.get("INTERVAL", "60"))
PUSH_EVERY = int(os.environ.get("PUSH_EVERY", "120"))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "345"))  # GitHub job limit = 360
GIT_PUSH = os.environ.get("GIT_PUSH") == "1"
ONCE = os.environ.get("ONCE") == "1"


def now_ms():
    return int(time.time() * 1000)


def iso_ms(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000) if s else None


def get(path, **params):
    params["key"] = KEY
    url = API + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def load_state(cfg):
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                s = json.load(f)
            if s.get("event", {}).get("start") == cfg["event"]["start"]:
                return s
        except Exception:
            pass
    return {"event": cfg["event"], "channels": cfg["channels"], "matches": cfg["matches"],
            "interval": INTERVAL, "streams": {}, "samples": [], "status": {}}


def tick(cfg, state):
    quota = 0
    candidates = set(v for v, s in state["streams"].items() if not s.get("end"))
    for ch in cfg["channels"]:
        uploads = "UU" + ch["id"][2:]
        try:
            pl = get("playlistItems", part="contentDetails", playlistId=uploads, maxResults=5)
            quota += 1
            for it in pl.get("items", []):
                candidates.add(it["contentDetails"]["videoId"])
        except Exception as e:
            print(f"playlist {ch['key']}: {e}", file=sys.stderr)

    ch_by_id = {c["id"]: c["key"] for c in cfg["channels"]}
    sample = {}
    ids = list(candidates)
    for i in range(0, len(ids), 50):
        vs = get("videos", part="snippet,liveStreamingDetails,statistics", id=",".join(ids[i:i + 50]))
        quota += 1
        for v in vs.get("items", []):
            ld = v.get("liveStreamingDetails")
            if not ld:
                continue  # คลิปธรรมดา
            key = ch_by_id.get(v["snippet"]["channelId"])
            if not key:
                continue
            vid = v["id"]
            is_live = v["snippet"].get("liveBroadcastContent") == "live"
            s = state["streams"].get(vid)
            if not s:
                if not is_live and not (ld.get("actualStartTime") and not ld.get("actualEndTime")):
                    continue  # สนใจเฉพาะไลฟ์ที่กำลังเกิดขึ้น
                s = state["streams"][vid] = {"key": key, "title": v["snippet"]["title"],
                                             "scheduled": iso_ms(ld.get("scheduledStartTime")),
                                             "start": iso_ms(ld.get("actualStartTime")) or now_ms(),
                                             "end": None, "peak": 0, "peakAt": None}
                print(f"+ live {key}: {s['title']}")
            s["title"] = v["snippet"]["title"]
            if ld.get("actualEndTime") or not is_live:
                if not s["end"]:
                    s["end"] = iso_ms(ld.get("actualEndTime")) or now_ms()
                    print(f"- ended {key}: {s['title']}")
                continue
            viewers = ld.get("concurrentViewers")
            viewers = int(viewers) if viewers is not None else None
            st = v.get("statistics", {})
            sample[vid] = [viewers, int(st.get("likeCount", 0) or 0), int(st.get("commentCount", 0) or 0),
                           int(st.get("viewCount", 0) or 0)]
            if viewers is not None and viewers > s["peak"]:
                s["peak"], s["peakAt"] = viewers, now_ms()

    state["samples"].append({"t": now_ms(), "d": sample})
    state["updated"] = now_ms()
    q = state["status"].get("quota", 0) + quota
    state["status"] = {"ok": True, "quota": q, "live": len(sample)}


def save(state):
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT)


def push():
    subprocess.run(["git", "add", OUT], check=True)
    subprocess.run(["git", "commit", "--amend", "-q", "-m", "data: live snapshot"], check=True)
    subprocess.run(["git", "push", "-q", "-f", "origin", "HEAD:data"], check=True)


def main():
    if not KEY:
        sys.exit("ไม่มี YT_API_KEY")
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    state = load_state(cfg)
    state["channels"], state["matches"], state["event"] = cfg["channels"], cfg["matches"], cfg["event"]
    started = time.time()
    end_ms = iso_ms(cfg["event"]["end"])
    last_push = 0
    while True:
        t0 = time.time()
        try:
            tick(cfg, state)
        except Exception as e:
            state["status"] = {**state.get("status", {}), "ok": False, "error": str(e)[:300]}
            print(f"tick error: {e}", file=sys.stderr)
        save(state)
        finished = ONCE or now_ms() > end_ms or (time.time() - started) / 60 > MAX_MINUTES
        if GIT_PUSH and (finished or time.time() - last_push >= PUSH_EVERY):
            try:
                push()
                last_push = time.time()
            except Exception as e:
                print(f"push error: {e}", file=sys.stderr)
        if finished:
            break
        time.sleep(max(1, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
