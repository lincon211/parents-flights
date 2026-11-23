#!/usr/bin/env python3
"""抓取并筛选父母赴加往返机票，生成网站数据。

数据来源：SerpApi 的 Google Flights 接口（环境变量 SERPAPI_KEY）。
输出：data/latest.json（本次结果与排名）、data/history.json（历次快照，用于追踪变化）。

用法：
  SERPAPI_KEY=xxx python3 scripts/fetch.py
  python3 scripts/fetch.py --offline tests/sample   # 用本地样例 JSON 测试逻辑
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---------------- 需求配置 ----------------
ORIGINS = "PVG,SHA,HGH"          # 上海浦东、上海虹桥、杭州萧山
DESTS = ["YVR", "YYZ"]           # 温哥华、多伦多分开查，保证两个目的地都有足够的候选
TRIPS = [
    {"id": "A", "label": "方案一：6月19日出发，6月27日返回", "out": "2027-06-19", "ret": "2027-06-27"},
]
CURRENCY = "CAD"
MAX_LAYOVER_MIN = 240            # 转机等待不超过4小时
RETURN_PROBES_PER_TRIP = int(os.environ.get("RETURN_PROBES", "4"))  # 每个日期×目的地深入查回程的合格去程数量
EXCLUDED_PROBES_PER_TRIP = int(os.environ.get("EXCLUDED_PROBES", "1"))  # 再查几个“便宜但被排除”的去程的回程，供网站上手动加回
TOP_N = 5

# 中国护照无需过境签即可在机场内转机的地区（经美国、台湾转机需签证或许可，排除）
TRANSIT_OK_PREFIX = {
    # 中国大陆国内航段（国内转机不涉及过境签）
    "PEK", "PKX", "SHA", "PVG", "HGH", "CAN", "SZX", "CTU", "TFU", "CKG", "XMN", "TAO",
    "NKG", "WUH", "XIY", "KMG", "SHE", "DLC", "TSN", "CSX", "FOC", "HRB", "CGO", "TNA", "NGB", "WNZ",
    # 香港、澳门
    "HKG", "MFM",
    # 韩国、日本（机场内转机免签）
    "ICN", "GMP", "PUS", "NRT", "HND", "KIX", "NGO", "FUK", "CTS",
    # 卡塔尔、阿联酋（中国护照免签，机场内转机无需签证）
    "DOH", "DXB", "AUH",
    # 加拿大境内（持加拿大签证入境后转国内航班）
    "YVR", "YYZ", "YYC", "YUL", "YEG", "YOW", "YWG",
}

AIRLINE_CN = {
    "Air Canada": "加拿大航空", "China Eastern": "东方航空", "China Southern": "南方航空",
    "Air China": "中国国际航空", "Hainan Airlines": "海南航空", "Capital Airlines": "首都航空",
    "Beijing Capital Airlines": "首都航空", "Sichuan Airlines": "四川航空", "Xiamen Airlines": "厦门航空",
    "Shanghai Airlines": "上海航空", "Juneyao Air": "吉祥航空", "Cathay Pacific": "国泰航空",
    "Korean Air": "大韩航空", "Asiana": "韩亚航空", "Asiana Airlines": "韩亚航空", "ANA": "全日空",
    "All Nippon Airways": "全日空", "Japan Airlines": "日本航空", "JAL": "日本航空", "WestJet": "西捷航空",
    "Air Canada Rouge": "加航胭脂", "Zipair": "ZIPAIR", "Hong Kong Airlines": "香港航空",
    "Air Premia": "Air Premia（韩国）", "EVA Air": "长荣航空", "Shenzhen Airlines": "深圳航空",
    "Tibet Airlines": "西藏航空", "Spring Airlines": "春秋航空",
}

AIRPORT_CN = {
    "PVG": "上海浦东", "SHA": "上海虹桥", "HGH": "杭州萧山", "YYZ": "多伦多皮尔逊", "YVR": "温哥华",
    "PEK": "北京首都", "PKX": "北京大兴", "CAN": "广州白云", "SZX": "深圳宝安", "CTU": "成都双流",
    "TFU": "成都天府", "CKG": "重庆江北", "XMN": "厦门高崎", "TAO": "青岛胶东", "NKG": "南京禄口",
    "HKG": "香港", "MFM": "澳门", "ICN": "首尔仁川", "GMP": "首尔金浦", "NRT": "东京成田",
    "HND": "东京羽田", "KIX": "大阪关西", "NGO": "名古屋", "YYC": "卡尔加里", "YUL": "蒙特利尔",
    "YEG": "埃德蒙顿", "DOH": "多哈", "DXB": "迪拜", "AUH": "阿布扎比", "YOW": "渥太华", "YWG": "温尼伯", "TPE": "台北桃园", "SEA": "西雅图",
    "SFO": "旧金山", "LAX": "洛杉矶", "JFK": "纽约肯尼迪", "ORD": "芝加哥", "DTW": "底特律",
}

BAG_FEE_MARKERS = ("checked baggage for a fee", "no checked bag", "checked bag for a fee")


# ---------------- 抓取 ----------------
def serpapi(params, offline_dir=None, tag=None):
    if offline_dir:
        p = Path(offline_dir) / f"{tag}.json"
        return json.loads(p.read_text()) if p.exists() else {}
    key = (os.environ.get("SERPAPI_KEY") or "").strip()
    if not key:
        sys.exit("缺少 SERPAPI_KEY 环境变量")
    base = {"engine": "google_flights", "currency": CURRENCY, "hl": "en", "gl": "ca",
            "travel_class": 1, "adults": 1, "api_key": key}
    base.update(params)
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(base)
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        if e.code == 401:
            sys.exit(f"SerpApi 拒绝了这个 Key（401）：{body}\n请检查仓库 Secret SERPAPI_KEY 是否为 serpapi.com/manage-api-key 页面上的 Private API Key。")
        print(f"SerpApi HTTP {e.code}：{body}", file=sys.stderr)
        return {"error": body}
    if data.get("error"):
        print("SerpApi 返回：", data["error"], file=sys.stderr)
    return data


def account_info(offline=None):
    """查询 SerpApi 剩余额度（这个接口不占用查询次数）。"""
    if offline:
        return None
    try:
        key = (os.environ.get("SERPAPI_KEY") or "").strip()
        with urllib.request.urlopen("https://serpapi.com/account.json?api_key=" + key, timeout=30) as r:
            a = json.loads(r.read())
        return {k: a.get(k) for k in ("plan_name", "searches_per_month", "plan_searches_left",
                                      "this_month_usage", "total_searches_left")}
    except Exception as e:  # 额度查询失败不影响主流程
        return {"error": str(e)[:200]}


def options(resp):
    return (resp.get("best_flights") or []) + (resp.get("other_flights") or [])


# ---------------- 解析与筛选 ----------------
def parse_leg(opt):
    segs = opt.get("flights") or []
    if not segs:
        return None
    lays = opt.get("layovers") or []
    leg = {
        "segments": [{
            "flight": s.get("flight_number", ""),
            "airline": s.get("airline", ""),
            "airline_cn": AIRLINE_CN.get(s.get("airline", ""), s.get("airline", "")),
            "from": s["departure_airport"]["id"], "to": s["arrival_airport"]["id"],
            "dep": s["departure_airport"].get("time"), "arr": s["arrival_airport"].get("time"),
            "duration": s.get("duration"), "aircraft": s.get("airplane", ""),
            "class": s.get("travel_class", ""),
        } for s in segs],
        "layovers": [{"airport": l.get("id"), "duration": l.get("duration", 0),
                      "overnight": bool(l.get("overnight"))} for l in lays],
        "total_duration": opt.get("total_duration") or sum(s.get("duration", 0) for s in segs),
        "price": opt.get("price"),
        "token": opt.get("departure_token"),
        "extensions": [e.lower() for e in (opt.get("extensions") or [])],
    }
    leg["key"] = "+".join(s["flight"].replace(" ", "") for s in leg["segments"])
    leg["from"] = leg["segments"][0]["from"]
    leg["to"] = leg["segments"][-1]["to"]
    leg["dep"] = leg["segments"][0]["dep"]
    leg["arr"] = leg["segments"][-1]["arr"]
    return leg


def reject_reason(leg):
    """返回不符合要求的原因（中文），符合则返回 None。"""
    for s in leg["segments"]:
        cls = (s["class"] or "").lower()
        if cls and "economy" not in cls:
            return "非经济舱"
        if "premium" in cls:
            return "非普通经济舱"
    for l in leg["layovers"]:
        if l["airport"] not in TRANSIT_OK_PREFIX:
            return f"经{AIRPORT_CN.get(l['airport'], l['airport'])}转机需签证或未确认免签"
        if (l["duration"] or 0) > MAX_LAYOVER_MIN:
            return "转机等待超过4小时"
    for s in leg["segments"][1:]:
        if s["from"] not in TRANSIT_OK_PREFIX:
            return "经停地需签证"
    if any(m in e for e in leg["extensions"] for m in BAG_FEE_MARKERS):
        return "不含免费托运行李"
    return None


def arrival_penalty(arr_time):
    """按加拿大当地落地时间给罚分（单位：小时，越小越好）。早上最好，晚上和深夜最差。"""
    if not arr_time:
        return 6, "未知"
    h = int(arr_time[11:13]) + int(arr_time[14:16]) / 60
    if 6 <= h < 12:
        return 0, "上午"
    if 12 <= h < 15:
        return 1.5, "中午"
    if 15 <= h < 18:
        return 3, "下午"
    if 18 <= h < 22:
        return 6, "晚上"
    return 9, "深夜/凌晨"


def build_pairs(trip, offline=None):
    """两个目的地分别查询后合并。每次刷新约 1日期×2目的地×(1+4+1)=12 次查询。"""
    outs, pairs, rejected, notes, excluded = [], [], {}, [], []
    for dest in DESTS:
        o, p, r, n, e = build_pairs_dest(trip, dest, offline)
        outs += o; pairs += p; notes += n; excluded += e
        for k, v in r.items():
            rejected[k] = rejected.get(k, 0) + v
    excluded = sorted(excluded, key=lambda x: x["price"] or 1e9)[:30]
    return outs, pairs, rejected, notes, excluded


def build_pairs_dest(trip, dest, offline=None):
    """对一个日期组合：查往返去程列表，再对重点去程查回程，返回 (所有去程, 有效组合, 排除统计)。"""
    rt = serpapi({"type": 1, "departure_id": ORIGINS, "arrival_id": dest,
                  "outbound_date": trip["out"], "return_date": trip["ret"]},
                 offline, f"{trip['id']}_{dest}_out")
    outs, rejected = [], {}
    for o in options(rt):
        leg = parse_leg(o)
        if not leg:
            continue
        why = reject_reason(leg)
        leg["valid"] = why is None
        leg["reject"] = why
        outs.append(leg)
        if why:
            rejected[why] = rejected.get(why, 0) + 1
    valid_outs = [l for l in outs if l["valid"] and l["token"]]

    # 选出最值得深入查回程的去程：价格最低 + 时间/落地最好的，各取一半
    def tscore(l):
        return l["total_duration"] / 60 + arrival_penalty(l["arr"])[0]
    by_price = sorted(valid_outs, key=lambda l: l["price"] or 1e9)
    by_time = sorted(valid_outs, key=tscore)
    probes = []
    for a, b in zip(by_time, by_price):
        for l in (a, b):
            if l not in probes:
                probes.append(l)
        if len(probes) >= RETURN_PROBES_PER_TRIP:
            break
    probes = probes[:RETURN_PROBES_PER_TRIP]
    # 便宜但不合格的去程（舱位不对的不算），也查回程，组合放进“排除区”，网站上可以手动加回
    cheap_bad = sorted([l for l in outs if not l["valid"] and l["token"] and "舱" not in (l["reject"] or "")],
                       key=lambda l: l["price"] or 1e9)[:EXCLUDED_PROBES_PER_TRIP]
    probes = probes + cheap_bad

    pairs, notes, excluded = [], [], []
    for i, ob in enumerate(probes):
        rr = serpapi({"type": 1, "departure_id": ORIGINS, "arrival_id": dest,
                      "outbound_date": trip["out"], "return_date": trip["ret"],
                      "departure_token": ob["token"]}, offline, f"{trip['id']}_{dest}_ret{i}")
        gf_url = (rr.get("search_metadata") or {}).get("google_flights_url")
        n_ok, bad = 0, []
        for r in options(rr):
            rleg = parse_leg(r)
            if not rleg:
                continue
            why = reject_reason(rleg)
            pen, slot = arrival_penalty(ob["arr"])
            pair = {
                "trip": trip["id"], "key": f"{trip['id']}|{ob['key']}|{rleg['key']}",
                "out": strip(ob), "ret": strip(rleg),
                "price": rleg["price"] or ob["price"], "price_est": not rleg["price"],
                "total_minutes": ob["total_duration"] + rleg["total_duration"],
                "arr_penalty": pen, "arr_slot": slot, "gf_url": gf_url,
                "out_date": trip["out"], "ret_date": trip["ret"],
            }
            reasons = []
            if ob["reject"]:
                reasons.append("去程" + ob["reject"])
            if why:
                reasons.append("回程" + why)
                rejected["回程" + why] = rejected.get("回程" + why, 0) + 1
                bad.append({"ret": rleg["key"], "price": rleg["price"], "reason": why,
                            "layovers": rleg["layovers"]})
            if reasons:
                if "舱" not in "".join(reasons):        # 舱位不对的组合不放进排除区
                    pair["reject"] = "；".join(reasons)
                    excluded.append(pair)
                continue
            n_ok += 1
            pairs.append(pair)
        notes.append({"trip": trip["id"], "out": ob["key"], "out_price": ob["price"],
                      "out_valid": ob["valid"], "out_reject": ob["reject"],
                      "out_from": ob["from"], "out_to": ob["to"], "valid_returns": n_ok,
                      "returns_seen": n_ok + len(bad), "gf_url": gf_url,
                      "rejected_returns": sorted(bad, key=lambda b: b["price"] or 1e9)[:5]})
    # 同一个排除组合只保留最便宜的前若干个，控制数据大小
    excluded = sorted(excluded, key=lambda p: p["price"] or 1e9)[:30]
    return outs, pairs, rejected, notes, excluded


def strip(leg):
    return {k: v for k, v in leg.items() if k not in ("token", "extensions")}


# ---------------- 排名 ----------------
def rank(pairs):
    if not pairs:
        return {"time": [], "price": [], "overall": []}
    for p in pairs:
        p["time_score"] = round(p["total_minutes"] / 60 + p["arr_penalty"], 2)
    ts = [p["time_score"] for p in pairs]
    ps = [p["price"] for p in pairs]
    tmin, pmin = min(ts), min(ps)
    # 按“比最好的多出多少”打分，不受个别天价票影响：
    # 时间比最短的多50%记满扣分，价格比最便宜的贵一倍记满扣分
    for p in pairs:
        nt = min(1, (p["time_score"] - tmin) / (tmin * 0.5)) if tmin else 0
        np_ = min(1, (p["price"] - pmin) / pmin) if pmin else 0
        p["overall_score"] = round(100 - 50 * nt - 50 * np_, 1)   # 满分100

    def top(key, reverse=False):
        out, per_outbound = [], {}
        for p in sorted(pairs, key=key, reverse=reverse):
            ok = p["trip"] + p["out"]["key"]
            if per_outbound.get(ok, 0) >= 2:      # 同一去程最多出现两次，保证多样性
                continue
            per_outbound[ok] = per_outbound.get(ok, 0) + 1
            out.append(p["key"])
            if len(out) >= TOP_N:
                break
        return out
    return {
        "time": top(lambda p: (p["time_score"], p["price"])),
        "price": top(lambda p: (p["price"], p["time_score"])),
        "overall": top(lambda p: (p["overall_score"], -p["price"]), reverse=True),
    }


# ---------------- 变化追踪 ----------------
def diff(prev, cur):
    changes = []
    if not prev:
        return changes
    po = {o["trip"] + "|" + o["key"]: o for o in prev.get("outbounds", [])}
    co = {o["trip"] + "|" + o["key"]: o for o in cur["outbounds"]}
    for k in co.keys() - po.keys():
        changes.append({"type": "新增航班", "trip": co[k]["trip"], "flight": co[k]["key"],
                        "detail": f"新出现去程 {co[k]['key']}，往返最低 {co[k]['price']} 加元"})
    for k in po.keys() - co.keys():
        changes.append({"type": "航班消失", "trip": po[k]["trip"], "flight": po[k]["key"],
                        "detail": f"去程 {po[k]['key']} 本次未出现（可能停售、售罄或取消）"})
    for k in co.keys() & po.keys():
        a, b = po[k].get("price"), co[k].get("price")
        if a and b and a != b:
            changes.append({"type": "降价" if b < a else "涨价", "trip": co[k]["trip"],
                            "flight": co[k]["key"], "from": a, "to": b,
                            "detail": f"去程 {co[k]['key']} 往返最低价 {a} → {b} 加元"})
    pp = {p["key"]: p for p in prev.get("pairs", [])}
    for p in cur["pairs"]:
        old = pp.get(p["key"])
        if old and old["price"] != p["price"]:
            changes.append({"type": "降价" if p["price"] < old["price"] else "涨价",
                            "trip": p["trip"], "flight": p["out"]["key"] + " / " + p["ret"]["key"],
                            "from": old["price"], "to": p["price"],
                            "detail": f"往返组合价格 {old['price']} → {p['price']} 加元"})
    prev_top = set(prev.get("rank_all", {}).get("overall", []))
    cur_top = set(cur["rank_all"]["overall"])
    if prev_top and prev_top != cur_top:
        changes.append({"type": "最优解更新", "trip": "", "flight": "",
                        "detail": "综合前五名单发生变化"})
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", help="用本地样例 JSON 目录代替在线查询")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    all_outs, all_pairs, rejected_all, trips_out, all_notes, all_excluded = [], [], {}, [], [], []
    for trip in TRIPS:
        outs, pairs, rej, notes, excl = build_pairs(trip, args.offline)
        all_notes += notes
        all_excluded += excl
        for o in outs:
            o["trip"] = trip["id"]
        all_outs += outs
        all_pairs += pairs
        for k, v in rej.items():
            rejected_all[k] = rejected_all.get(k, 0) + v
        r = rank(pairs)
        trips_out.append({**trip, "rank": r, "outbound_count": len(outs),
                          "valid_outbound_count": sum(o["valid"] for o in outs),
                          "pair_count": len(pairs)})

    cur = {
        "updated": now, "currency": CURRENCY, "trips": trips_out,
        "outbounds": [{k: v for k, v in strip(o).items() if k != "segments"} | {"segments": o["segments"]}
                      for o in all_outs],
        "pairs": all_pairs, "rank_all": rank(all_pairs), "rejected": rejected_all,
        "airports": AIRPORT_CN, "probe_notes": all_notes, "excluded_pairs": all_excluded, "quota": account_info(args.offline),
    }
    # rank() 会给 pairs 写入分数，按全部组合重新计算一次以统一口径
    DATA.mkdir(exist_ok=True)
    latest_p, hist_p = DATA / "latest.json", DATA / "history.json"
    prev = json.loads(latest_p.read_text()) if latest_p.exists() else None
    if prev and not prev.get("pairs") and not prev.get("outbounds"):
        prev = None
    if not all_outs:
        print("本次没有取到任何航班，保留上一次数据。", file=sys.stderr)
        sys.exit(1)
    cur["changes"] = diff(prev, cur)

    hist = json.loads(hist_p.read_text()) if hist_p.exists() else []
    hist.append({
        "time": now,
        "best_price": {t["id"]: min([p["price"] for p in all_pairs if p["trip"] == t["id"]] or [None],
                                    key=lambda x: x or 1e9) for t in TRIPS},
        "outbound_count": {t["id"]: sum(1 for o in all_outs if o["trip"] == t["id"] and o["valid"]) for t in TRIPS},
        "prices": {p["key"]: p["price"] for p in all_pairs + all_excluded if not p.get("price_est")},
        "changes": cur["changes"],
    })
    hist = hist[-200:]
    latest_p.write_text(json.dumps(cur, ensure_ascii=False, indent=1))
    hist_p.write_text(json.dumps(hist, ensure_ascii=False, indent=1))
    print(f"完成：去程 {len(all_outs)} 个，有效往返组合 {len(all_pairs)} 个，变化 {len(cur['changes'])} 条")


if __name__ == "__main__":
    main()
