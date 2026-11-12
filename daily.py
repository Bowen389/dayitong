import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from math import isfinite
from time import sleep
from decimal import Decimal, ROUND_HALF_UP
import re
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from exchange_calendars import get_calendar


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
        anchors = eligible
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
        def evidence_score(evidence):
            if evidence["route"] == "top/LH":
                return 20
            if evidence["family"] == "量价转强":
                return 13
            return 10

        setup, evidence = max(
            pairs, key=lambda pair: setup_quality(pair[0]) + evidence_score(pair[1])
        )
        parts = dict(主信号=setup_quality(setup), 辅证=evidence_score(evidence),
                     因子分=round(factor_score * .35, 3))
        confirmation_board.append(dict(base, anchor=setup, evidence=evidence,
                                       families=families, parts=parts,
                                       score=round(sum(parts.values()), 3)))

    order = lambda item: (-item["score"], item["code"])
    return {"factor": sorted(factor_board, key=order)[:3],
            "confirmation": sorted(confirmation_board, key=order)[:3]}


MAIN_BOARD = re.compile(r"(?:600|601|603|605|000|001|002|003)\d{3}")


def top5_factors(day, code):
    params = urlencode(dict(
        secid=f"{int(code.startswith('6'))}.{code}", klt=101, fqt=0,
        beg=(date.fromisoformat(day) - timedelta(days=100)).strftime("%Y%m%d"),
        end=day.replace("-", ""), fields1="f1,f2,f3,f4,f5,f6",
        fields2="f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    ))
    for attempt in range(3):
        try:
            request = Request(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{params}",
                              headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=8) as response:
                data = json.load(response)["data"]
            bars = [line.split(",") for line in data["klines"]]
            dates = [bar[0] for bar in bars]
            if len(bars) < 21 or dates[-1] != day or dates != sorted(set(dates)):
                raise ValueError("日线不足、日期不符或存在重复")
            current = bars[-1]
            if re.search(r"ST|退", data["name"], re.I):
                return None
            close, volume, amount, turnover = (float(current[i]) for i in (2, 5, 6, 10))
            avg_volume = sum(float(bar[5]) for bar in bars[-6:-1]) / 5
            low20 = min(float(bar[4]) for bar in bars[-21:-1])
            high20 = max(float(bar[3]) for bar in bars[-21:-1])
            low10 = min(float(bar[4]) for bar in bars[-11:-1])
            high10 = max(float(bar[3]) for bar in bars[-11:-1])
            values = (close, volume, amount, turnover, avg_volume, low20, high20, low10, high10)
            if not all(isfinite(v) for v in values) or min(close, volume, amount, avg_volume, low20, low10) <= 0:
                raise ValueError("行情字段无效")
            position = (close - low20) / (high20 - low20) if high20 > low20 else None
            amplitude = high10 / low10 - 1
            score = (2 * (volume / avg_volume >= 2.5) + (10 <= turnover < 15)
                     + (position is not None and .60 <= position < .85)
                     + (.25 <= amplitude < .35))
            reference = Decimal(current[2]) - Decimal(current[9])
            upper_limit = (reference * Decimal("1.10")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
            return dict(score=score, amount=amount, close=close,
                        closed_at_limit=Decimal(current[2]) >= upper_limit)
        except (OSError, ValueError, KeyError, TypeError, IndexError, ArithmeticError) as exc:
            error = str(exc)
            if attempt < 2:
                sleep(attempt + 1)
    raise ValueError(error)


def build_top5(day, candidates, fetch=top5_factors):
    pool = [item for item in candidates if item["votes"] > 0
            and MAIN_BOARD.fullmatch(item["code"])
            and not re.search(r"ST|退", item["name"], re.I)]
    def load(item):
        try:
            factors = fetch(day, item["code"])
            return ({**{key: item[key] for key in ("code", "name", "votes")}, **factors}
                    if factors is not None else None), None
        except ValueError as exc:
            return None, str(exc)

    rows, missing = [], {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for item, (row, error) in zip(pool, executor.map(load, pool)):
            if error is not None:
                missing[item["code"]] = error
            elif row is not None:
                rows.append(row)
    rows.sort(key=lambda item: (-item["votes"], -item["score"], -item["amount"], item["code"]))
    return dict(status="incomplete" if missing else "ready",
                candidates=rows[:5] if not missing else [], missing=missing,
                pool=rows)


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
        if key == "v3" and reason:
            if meta.get("gate_allow") is False and not section.get("error") and not section.get("provisional"):
                reason = "仅观察：沪深300未站上MA20"
            raw = meta.get("raw_candidates") or []
            seen = {item.get("code") for item in items}
            items = items + [item for item in raw if item.get("code") not in seen]
            health["top/V3"] = f"{reason}，展示 {len(items)} 只"
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
    return dict(date=day, sources=health, candidates=candidates, etfs=etfs, systems=rank_systems(candidates), top5=build_top5(day, candidates), h1=zonghe.get("H1", {}))


def execution_plan(report):
    top5 = report["top5"]
    h1 = report["h1"]
    lines = ["", "## 次日开盘执行 · B → A → H1 → Top5", "",
             "9:25 竞价结束后按实际开盘价与当日参考昨收计算缺口。"
             "依次补剩余名额，同股只买一次，每天最多两只。"
             "跳过停牌、开在涨跌停价及预算内买不到一手的股票。", "",
             "1. **B 多信号共振** · 至少两个有效家族，信号日收盘未涨停，开盘缺口 ≤ +2%。"
             "先按家族数降序，再按缺口升序，最后按代码升序。",
             "2. **A 深低开** · 至少一个有效家族，开盘缺口 ≤ −5%。"
             "按缺口升序，同缺口按代码升序。",
             "3. **H1** · 沿用源策略的门控与路线。BF 先按原顺序确认，"
             "BF 无可成交票才检查 FB，源策略合计最多一只。",
             "4. **Top5** · 从盘后已锁定的五只里，按开盘缺口由低到高"
             "补入缺口严格低于 −1% 的股票。同缺口沿用盘后顺序。", "",
             "每笔含买入费约 1 万元，按 100 股向下取整。开盘买入后下一交易日收盘退出，"
             "停牌或收盘跌停则顺延。", ""]
    if top5["missing"]:
        lines.extend(["**行情待补齐，A/B 候选和 Top5 暂未核定。**", ""])
    else:
        for label, rows in (("B 盘后候选", [r for r in top5["pool"]
                                             if r["votes"] >= 2 and not r["closed_at_limit"]]),
                            ("A 盘后观察池", top5["pool"]),
                            ("Top5 盘后候选", top5["candidates"])):
            names = "、".join(f"{r['code']} {r['name']}" for r in rows) or "暂无"
            lines.append(f"- **{label}** · {names}")
    if h1.get("status") == "待开盘":
        lines.append("- **H1 源策略** · " + h1["note"])
        for r in h1["candidates"]:
            lines.append(f"  - {r['code']} {r['name']} · {r['route']}")
    else:
        lines.append(f"- **H1 源策略** · {h1.get('status') or '数据待核验'}")
    return lines


def markdown(report):
    lines = [f"# {report['date']} 大一统盘后预案", "",
             "两套系统仅用盘后信息定榜，不使用次日开盘缺口。下方各来源的原始条件仅供对照。", "",
             "## 数据来源", ""]
    lines.extend(f"- {key} · {status}" for key, status in report["sources"].items())
    top5 = report["top5"]
    lines.extend(["", "## H1 补位用盘后 Top5", ""])
    if top5["status"] == "incomplete":
        lines.append("行情待补齐，暂未锁榜。缺失代码：" + "、".join(top5["missing"]))
    elif not top5["candidates"]:
        lines.append("暂无合格候选。")
    else:
        lines.extend(["| 排名 | 股票 | 有效家族数 | 量价分 | 当日成交额 | 收盘价 |",
                      "| --- | --- | ---: | ---: | ---: | ---: |"])
        for rank, item in enumerate(top5["candidates"], 1):
            lines.append(f"| {rank} | {item['code']} {item['name']} | {item['votes']} | "
                         f"{item['score']} | {item['amount']/1e8:.2f} 亿 | {item['close']:.2f} |")
        lines.append("\n次日仅从这张榜单中选缺口严格低于 −1% 的可成交股票，按缺口从低到高补 H1 剩余名额。")
    for key, title in (("factor", "方案一 · 因子选股"),
                       ("confirmation", "方案二 · 多信号重评分")):
        lines.extend(["", f"## {title}", ""])
        for rank, item in enumerate(report["systems"][key], 1):
            role = "盘后Top1" if rank == 1 else f"观察第 {rank} 名"
            parts = "、".join(f"{name} {value:+g}" for name, value in item["parts"].items())
            lines.append(f"- **{item['code']} {item['name']}** · {role} · {item['score']:g} 分")
            lines.append(f"  - {parts}。主信号 {item['anchor']['route']}")
            if key == "confirmation":
                lines.append(f"  - 辅证 {item['evidence']['route']}")
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
    lines.extend(execution_plan(report))
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
