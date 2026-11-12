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


def bounded(value):
    return max(0.0, min(1.0, value))


def stock_factors(signals):
    factors = {}
    for signal in signals:
        detail = signal["detail"]
        route = signal["route"]
        if route == "top/LH":
            amount = detail.get("qualifying_amount_yuan")
            ratio = detail.get("volume_ratio_20")
            position = detail.get("low_multiple_120")
            if isinstance(amount, (int, float)):
                factors["机构买入"] = bounded(amount / 100_000_000)
            if isinstance(ratio, (int, float)) and ratio > 0:
                factors["量能适度"] = bounded(1 - abs(ratio - 1.5) / 1.5)
            if isinstance(position, (int, float)) and position >= 1:
                factors["位置克制"] = bounded((4 - position) / 3)
        if route == "top/BF":
            bits = detail.get("score_bits")
            if isinstance(bits, list) and len(bits) == 3:
                factors["反包结构"] = (float(bits[0]) + float(bits[2])) / 2
        amount = detail.get("S") or detail.get("mean")
        if isinstance(amount, (int, float)) and amount > 0:
            factors["流动性"] = max(factors.get("流动性", 0),
                                  bounded(amount / 500_000_000))
        if route.startswith("2j3/") or route == "主力捉妖":
            try:
                turnover = float(detail.get("turnover_pct", detail.get("turnover")))
            except (TypeError, ValueError):
                continue
            factors["换手适度"] = bounded(1 - abs(turnover - 8) / 8)
    return factors


FACTOR_WEIGHTS = {"机构买入": 25, "量能适度": 15, "位置克制": 15,
                  "反包结构": 20, "流动性": 15, "换手适度": 10}
SETUP_QUALITY = {"top/BF": 20, "zonghe/H1": 18, "top/FB": 17,
                 "ceshi/FB_DUAL_K4": 17, "zonghe/P2": 16, "2j3": 15,
                 "top/V3": 15, "zonghe/S2": 14, "top/S2": 13,
                 "主力捉妖": 13, "top/LH": 11}


def setup_quality(signal):
    route = signal["route"]
    key = route.split("/")[0] if route.startswith("2j3/") else (
        "zonghe/S2" if route.startswith("zonghe/S2_") else route)
    score = SETUP_QUALITY.get(key, 0)
    if signal["detail"].get("category") == "B":
        score -= 3
    return max(0, score - min(signal["rank"] - 1, 4))


def rank_systems(candidates):
    factor_board = []
    confirmation_board = []
    for candidate in candidates:
        code = candidate["code"]
        name = candidate["name"]
        if not code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
            continue
        if name.upper().replace(" ", "").startswith(("ST", "*ST", "SST", "S*ST")):
            continue
        eligible = [signal for signal in candidate["signals"] if not signal["reason"]]
        anchors = [signal for signal in eligible if entry_windows(signal)]
        if not anchors:
            continue
        anchor = max(anchors, key=setup_quality)
        factors = stock_factors(eligible)
        contributions = {key: round(FACTOR_WEIGHTS[key] * value, 3)
                         for key, value in factors.items()}
        factor_score = round(sum(contributions.values()), 3)
        base = dict(code=code, name=name, anchor=anchor)
        if len(factors) >= 2:
            factor_board.append(dict(base, score=factor_score, parts=contributions))

        families = sorted({signal["family"] for signal in eligible})
        if len(families) < 2:
            continue
        pairs = [(setup, evidence) for setup in anchors for evidence in eligible
                 if setup["family"] != evidence["family"]]
        def joint_windows(setup, evidence):
            return [joint for left in entry_windows(setup) for right in entry_windows(evidence)
                    if (joint := overlap(left, right)) is not None]

        def evidence_score(evidence):
            if evidence["route"] == "top/LH":
                return 20
            if evidence["family"] == "量价转强":
                return 13
            return 10

        def pair_score(pair):
            setup, evidence = pair
            if joint_windows(setup, evidence):
                compatibility = 15
            else:
                compatibility = -15 if entry_windows(evidence) else 0
            return setup_quality(setup) + evidence_score(evidence) + compatibility

        setup, evidence = max(pairs, key=pair_score)
        windows = joint_windows(setup, evidence)
        parts = dict(主买点=setup_quality(setup), 辅证=evidence_score(evidence),
                     开盘兼容=15 if windows else -15 if entry_windows(evidence) else 0,
                     因子分=round(factor_score * .35, 3))
        confirmation_board.append(dict(base, anchor=setup, evidence=evidence,
                                       families=families, windows=windows, parts=parts,
                                       score=round(sum(parts.values()), 3)))

    order = lambda item: (-item["score"], item["code"])
    return {"factor": sorted(factor_board, key=order)[:3],
            "confirmation": sorted(confirmation_board, key=lambda item: (not item["windows"], *order(item)))[:3]}


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
    return dict(date=day, sources=health, candidates=candidates, etfs=etfs, systems=rank_systems(candidates))


def markdown(report):
    lines = [f"# {report['date']} 大一统盘后预案", "",
             "票数为盘后预估。每个信号家族最多一票，所有开盘条件仍待核对。", "",
             "## 数据来源", ""]
    lines.extend(f"- {key} · {status}" for key, status in report["sources"].items())
    for key, title in (("factor", "方案一 · 因子选股"),
                       ("confirmation", "方案二 · 多信号重评分")):
        lines.extend(["", f"## {title}", ""])
        for rank, item in enumerate(report["systems"][key], 1):
            role = ("Top1 待开盘核验" if rank == 1 else f"观察第 {rank} 名")
            if key == "confirmation" and not item["windows"]:
                role = f"观察第 {rank} 名（共同区间未确认）"
            parts = "、".join(f"{name} {value:+g}" for name, value in item["parts"].items())
            lines.append(f"- **{item['code']} {item['name']}** · {role} · {item['score']:g} 分")
            lines.append(f"  - {parts}。主买点 {item['anchor']['route']} · {item['anchor']['rule']}")
            if key == "confirmation":
                evidence = item["evidence"]
                lines.append(f"  - 辅证 {evidence['route']} · {evidence['rule']}")
                windows = " 或 ".join(f"{'[' if a else '('}{lo:+g}%, {hi:+g}%{']' if b else ')'}"
                                      for lo, hi, a, b in item["windows"])
                lines.append(f"  - 共同缺口 {windows or '未核实共同区间，仅观察'}")
        if not report["systems"][key]:
            lines.append("暂无合格候选。")
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
