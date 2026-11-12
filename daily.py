import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from exchange_calendars import get_calendar


# Bounds are percentage points, with endpoint inclusion recorded explicitly.
def entry_windows(signal):
    route = signal["route"]
    detail = signal["detail"]
    arm = str(detail.get("arm") or detail.get("route") or detail.get("category") or "")
    if route == "top/BF" or (route == "zonghe/H1" and arm.startswith("BF")):
        return [(-10, 8, False, True)]
    if route == "zonghe/P2":
        return [(-5, -2, False, True)]
    if route.startswith("2j3/"):
        return [(2, 5, True, True)]
    if route == "主力捉妖":
        return [(-2, -1, True, False), (0, 3, True, False)]
    if route in {"top/FB", "ceshi/FB_DUAL_K4", "zonghe/H1"}:
        if arm.startswith("C2"):
            return [(-2.5, 5, True, True)]
        if arm.startswith("C1"):
            return [(-5, -2, True, True)]
    if route == "top/V3" or route.startswith("zonghe/S2_"):
        return [(-5, 6, True, True)]
    if route == "top/S2":
        return [(-10, 9.85, False, False)]
    if route == "top/LH":
        return [(-10, 10, False, False)]
    return []


def overlap(left, right):
    low = max(left[0], right[0])
    high = min(left[1], right[1])
    include_low = all(window[0] != low or window[2] for window in (left, right))
    include_high = all(window[1] != high or window[3] for window in (left, right))
    if low < high or (low == high and include_low and include_high):
        return (low, high, include_low, include_high)
    return None


def shortlist(candidates):
    # Compare entry theses first. Correlated price signals can confirm,
    # but an institutional disclosure supplies a different type of evidence.
    priority = {"top/BF": 0, "zonghe/H1": 1, "top/FB": 2,
                "ceshi/FB_DUAL_K4": 2, "zonghe/P2": 3, "2j3": 4,
                "top/V3": 5, "zonghe/S2": 5, "top/S2": 6, "主力捉妖": 7}
    plans = []
    for candidate in candidates:
        code = candidate["code"]
        if not code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
            continue
        if "ST" in candidate["name"].upper().replace(" ", ""):
            continue
        available = [signal for signal in candidate["signals"] if not signal["reason"]]
        best = None
        for anchor in available:
            route = anchor["route"]
            key = "2j3" if route.startswith("2j3/") else "zonghe/S2" if route.startswith("zonghe/S2_") else route
            if key not in priority:
                continue
            for evidence in available:
                if evidence["family"] == anchor["family"]:
                    continue
                windows = [joint for left in entry_windows(anchor)
                           for right in entry_windows(evidence)
                           if (joint := overlap(left, right)) is not None]
                if not windows:
                    continue
                detail = anchor["detail"]
                tier = detail.get("category") or str(detail.get("route", "")).removeprefix("BF ")
                grade = {"A": 0, "B": 1}.get(tier, 0)
                institutional = evidence["route"] == "top/LH"
                quality = (-int(institutional), priority[key], grade,
                           -float(detail.get("S") or 0), anchor["rank"], code)
                if best is None or quality < best[0]:
                    best = (quality, dict(code=code, name=candidate["name"],
                                          anchor=anchor, evidence=evidence, windows=windows))
        if best is not None:
            plans.append(best)
    chosen = []
    used_families = set()
    for _, plan in sorted(plans, key=lambda pair: pair[0]):
        family = plan["anchor"]["family"]
        if family in used_families:
            continue
        chosen.append(plan)
        used_families.add(family)
        if len(chosen) == 3:
            break
    return chosen


def build(day):
    rows = defaultdict(list)
    health = {}
    etfs = {}

    def read(label, repo, path, date_key="date"):
        url = f"https://raw.githubusercontent.com/Bowen389/{repo}/main/{path}"
        try:
            with urlopen(url, timeout=20) as response:
                data = json.load(response)
            if data.get(date_key) != day:
                health[label] = f"日期不符（{data.get(date_key)}）"
                return {}
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            health[label] = f"待补齐（{error}）"
            return {}
        health[label] = "已读取"
        return data

    def add(items, family, route, rule, blocked="", limit=None):
        for rank, item in enumerate(items, 1):
            code = str(item.get("code", ""))
            if len(code) != 6 or not code.isascii() or not code.isdigit():
                continue
            reason = blocked
            if limit is not None and rank > limit and not reason:
                reason = "超出该路线执行名次"
            if route == "top/BF":
                if item.get("previous_broken_limit"):
                    reason = "排除：信号日前一交易日触板未封"
                elif not item.get("bf_static_ok") and not reason:
                    reason = "排除：BF盘后个股预筛未通过"
            rows[code].append(dict(
                name=item.get("name", code), family=family, route=route,
                rule=rule, reason=reason, rank=rank, detail=item,
            ))

    daily = f"reports/{day}.json"
    top = read("top", "top", daily).get("strategies", {})
    routes = {
        "lh": ("机构", "开盘避开涨跌停及停牌"),
        "n3": ("机构", "与龙虎榜同家族，按N3原门控及成交条件"),
        "v3": ("趋势", "缺口[-5%, +6%]"),
        "s2": ("趋势", "开盘低于昨收×1.0985"),
        "bf": ("反包", "A优先，同档按20日均额降序，开盘缺口至多+8%"),
        "fb": ("首板", "C2[-2.5%, +5%]，C1[-5%, -2%]"),
    }
    for key, (family, rule) in routes.items():
        section = top.get(key, {})
        meta = section.get("meta") or {}
        reason = section.get("error") or ("数据待核验" if section.get("provisional") else "")
        if reason:
            health[f"top/{key}"] = str(reason)
        gate = {"bf": "market_gate_pass", "v3": "gate_allow"}.get(key)
        if gate and meta.get(gate) is not True:
            reason = reason or "市场门控未通过或待核验"
        items = section.get("candidates") or []
        if key == "bf":
            items = items + [
                item for item in meta.get("raw_candidates", [])
                if item.get("previous_broken_limit")
                and item.get("code") not in {row.get("code") for row in items}
            ]
        add(items, family, f"top/{key.upper()}", rule, str(reason))

    zonghe = read("zonghe", "zonghe", daily).get("strategies", {})
    for key, section in zonghe.items():
        for rank, item in enumerate(section.get("candidates") or [], 1):
            arm = str(item.get("route", ""))
            family = ("首板" if key == "P2" or (key == "H1" and not arm.startswith("BF"))
                      else "反包" if key == "H1" else "趋势")
            status = section.get("status")
            reason = "" if status == "待开盘" else str(status or "状态待核验")
            if key == "H1" and family == "反包":
                matching = [vote for vote in rows[item["code"]] if vote["route"] == "top/BF"]
                if any(vote["detail"].get("previous_broken_limit") for vote in matching):
                    reason = "排除：信号日前一交易日触板未封"
                elif not matching:
                    reason = reason or "BF盘后个股排除条件待交叉核验"
            if key.startswith("S2_") and rank > 1:
                reason = reason or "路线只执行Top1"
            add([item], family, f"zonghe/{key}", section.get("note") or "按源策略", reason)

    ceshi = read("ceshi", "ceshi", "reports/state.json")
    for key, items in ceshi.get("candidates", {}).items():
        family = "首板" if key == "FB_DUAL_K4" else "反包"
        rule = ("C2[-2.5%, +5%]，C1[-5%, -2%]" if family == "首板"
                else "按源策略的分档、开盘缺口和成交约束")
        paused = ceshi.get("book", {}).get(key, {}).get("paused")
        add(items, family, f"ceshi/{key}", rule, "源策略暂停开仓" if paused else "")

    s9 = read("s9-duoyinzi", "s9-duoyinzi", daily, "asof")
    for key, section in s9.get("strategies", {}).items():
        if key not in {"champion", "balanced", "runner", "original", "wild"}:
            etfs[key] = section
            continue
        reason = ""
        if section.get("status") != "ok" or section.get("asof") != day:
            reason = "数据待核验"
        elif section.get("gate_on") is not True:
            reason = "市场门控关闭"
        add(section.get("picks") or [], "趋势", f"S9/{key}",
            "核对源策略开盘成交条件", reason, limit=1 if key == "champion" else None)

    monster = read("zhulizhuoyao", "zhulizhuoyao", f"reports/{day}-close.json")
    add(monster.get("candidates", []), "量价转强", "主力捉妖",
        "优先[-2%, -1%)，其次[0%, +3%)",
        "" if monster.get("stage") == "close" else "盘后阶段待核验")
    ladder = read("2j3", "2j3", f"state/{day}.json")
    add(ladder.get("candidates", []), "首板", f"2j3/{ladder.get('route', '待核验')}",
        "缺口[+2%, +5%]，高开幅度降序",
        "" if ladder.get("status") == "scanned" else "源策略门控或扫描状态待核验")

    health["首板最低缺口Top1"] = "缺少独立同日信号源，尚未接入扫描"
    candidates = []
    for code, signals in rows.items():
        families = sorted({signal["family"] for signal in signals if not signal["reason"]})
        candidates.append(dict(code=code, name=signals[0]["name"], votes=len(families),
                               families=families, signals=signals))
    candidates.sort(key=lambda item: (-item["votes"], item["code"]))
    return dict(date=day, sources=health, candidates=candidates, etfs=etfs, shortlist=shortlist(candidates))


def markdown(report):
    lines = [f"# {report['date']} 大一统盘后预案", "",
             "票数为盘后预估。每个信号家族最多一票，所有开盘条件仍待核对。", "",
             "## 数据来源", ""]
    lines.extend(f"- {key} · {status}" for key, status in report["sources"].items())
    lines.extend(["", "## 次日选股预案（最多3只）", ""])
    if not report["shortlist"]:
        lines.append("暂无满足跨家族确认且开盘区间相交的候选。")
    for plan in report["shortlist"]:
        windows = " 或 ".join(f"{'[' if a else '('}{lo:+g}%, {hi:+g}%{']' if b else ')'}"
                              for lo, hi, a, b in plan["windows"])
        anchor = plan["anchor"]
        evidence = plan["evidence"]
        lines.append(f"- **{plan['code']} {plan['name']}** · 主买点 {anchor['route']} · 辅证 {evidence['route']} · 共同缺口 {windows}")
        lines.append(f"  - 仍需复核主路线最终排名、停牌与涨跌停、整手及现金。退出按主路线。")
    for title, check in (("两类及以上预计信号", lambda n: n >= 2),
                         ("单类预计信号", lambda n: n == 1),
                         ("仅观察与排除", lambda n: n == 0)):
        lines.extend(["", f"## {title}", ""])
        selected = [item for item in report["candidates"] if check(item["votes"])]
        if not selected:
            lines.append("暂无。")
        for item in selected:
            lines.append(f"- **{item['code']} {item['name']}** · {item['votes']} 票")
            for signal in item["signals"]:
                detail = signal["detail"]
                grade = f" {detail['category']}档 {detail.get('score', '—')}/3" if signal["route"] == "top/BF" and detail.get("category") else ""
                result = signal["reason"] or f"待确认 · {signal['rule']}"
                lines.append(f"  - {signal['route']}{grade} · {result}")
    lines.extend(["", "## ETF（独立展示）", ""])
    for key, section in report["etfs"].items():
        codes = ", ".join(map(str, section.get("target_codes") or [])) or "—"
        lines.append(f"- {key} · 目标 {codes} · 进攻门控 {section.get('gate_on')}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    today = now.date().isoformat()
    day = parser.parse_args().date or today
    if day > today or (day == today and now.hour < 15):
        parser.error("请选择已收盘日期")
    if not get_calendar("XSHG").is_session(day):
        print(f"{day} 休市")
        return
    report = build(day)
    folder = Path("reports")
    folder.mkdir(exist_ok=True)
    for stem in (day, "latest"):
        target = folder / f"{stem}.json"
        if stem == "latest" and target.exists() and json.loads(target.read_text())["date"] > day:
            continue
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (folder / f"{stem}.md").write_text(markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
