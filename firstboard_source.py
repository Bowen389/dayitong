"""Independent preselection using the existing ptrade_firstboard_top1.py rules.

This exports a watchlist, not an opening rank or a reconstructed account.
Next-session circulating capitalization, status and actual opening prices
must still be checked as in the original PTrade implementation.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import json
from pathlib import Path
import re
import time
import akshare as ak
import pandas as pd

PREFIX = re.compile(r'(600|601|603|605|000|001|002|003)\d{3}')


def eligible(frame):
    if len(frame) < 6 or not frame['volume'].tail(6).gt(0).all():
        return False
    from exchange_calendars import get_calendar
    calendar=get_calendar('XSHG')
    end=frame.date.iloc[-1]
    expected=calendar.sessions[calendar.sessions<=end][-6:]
    expected=pd.DatetimeIndex(expected).tz_localize(None)
    if list(frame.date.tail(6)) != list(expected):
        return False
    current = frame.iloc[-1]
    if not 300_000_000 <= current['amount'] <= 1_900_000_000:
        return False
    window = frame.tail(5)
    amplitude = (window.high.max()-window.low.min())/frame.close.iloc[-6]
    age = (frame.date.iloc[-1]-frame.loc[frame.volume.gt(0),'date'].iloc[0]).days
    return bool(age >= 50 and amplitude <= .20)


def history(day, code):
    target=Path('data/firstboard')/f'{day}-{code}.csv'
    start=(date.fromisoformat(day)-timedelta(days=160)).strftime('%Y%m%d')
    def validate(frame):
        frame=frame.rename(columns={'日期':'date','开盘':'open','最高':'high','最低':'low','收盘':'close','成交量':'volume','成交额':'amount'})
        required=['date','open','high','low','close','volume','amount']
        if not set(required).issubset(frame.columns) or frame.empty:
            raise ValueError('日线字段缺失')
        frame=frame[required].copy()
        frame['date']=pd.to_datetime(frame.date)
        frame=frame[frame.date.le(pd.Timestamp(day))].drop_duplicates('date').sort_values('date')
        if frame.empty or str(frame.date.iloc[-1].date()) != day:
            raise ValueError('缺少信号日行情')
        for key in required[1:]:
            frame[key]=pd.to_numeric(frame[key],errors='coerce')
        if frame[required[1:]].isna().any().any() or frame.close.le(0).any():
            raise ValueError('日线数值异常')
        return frame
    if target.exists():
        try:return validate(pd.read_csv(target))
        except Exception:pass
    errors=[]
    symbol=('sh' if code.startswith('6') else 'sz')+code
    for fn,kwargs in [(ak.stock_zh_a_hist_tx,{'symbol':symbol}), (ak.stock_zh_a_hist_em,{'symbol':code})]:
        try:
            frame=validate(fn(start_date=start,end_date=day.replace('-',''),adjust='',**kwargs))
            target.parent.mkdir(parents=True,exist_ok=True)
            frame.to_csv(target,index=False)
            return frame
        except Exception as exc:errors.append(str(exc))
    raise ValueError('; '.join(errors))


def build(day):
    errors={}
    candidates=[]
    try:
        cache=Path('data/firstboard')/f'{day}-pool.csv'
        try:
            pool=ak.stock_zt_pool_em(date=day.replace('-',''))
            if not {'代码','名称','连板数'}.issubset(pool.columns):raise ValueError('涨停池字段缺失')
            cache.parent.mkdir(parents=True,exist_ok=True)
            pool.to_csv(cache,index=False)
        except Exception:
            if not cache.exists():raise
            pool=pd.read_csv(cache,dtype={'代码':str})
        pool['代码']=pool['代码'].astype(str).str.zfill(6)
        pool=pool[pool['代码'].str.fullmatch(PREFIX)&~pool['名称'].str.contains('ST|退',case=False,na=False)&pd.to_numeric(pool['连板数']).eq(1)]
        if '成交额' in pool:
            amount=pd.to_numeric(pool['成交额'],errors='coerce')
            pool=pool[amount.isna()|amount.between(300_000_000,1_900_000_000)]
        def load(row):
            code=row['代码']
            try:
                frame=history(day,code)
                errors.pop(code,None)
                if not eligible(frame):return None
                latest=frame.iloc[-1]
                return dict(code=code,name=row['名称'],close=float(latest.close),amount=float(latest.amount))
            except Exception as exc:
                errors[code]=str(exc)
                return None
        with ThreadPoolExecutor(max_workers=4) as executor:
            candidates=[r for r in executor.map(load,pool.to_dict('records')) if r is not None]
        for attempt in range(2):
            if not errors:break
            time.sleep(2)
            retry=pool[pool['代码'].isin(list(errors))].to_dict('records')
            with ThreadPoolExecutor(max_workers=2) as executor:
                candidates.extend(r for r in executor.map(load,retry) if r is not None)
        candidates.sort(key=lambda r:(-r['amount'],r['code']))
        count=len(pool)
    except Exception as exc:
        errors['pool']=str(exc)
        count=0
    result=dict(date=day,status='partial' if errors else 'complete',pool_count=count,errors=errors,
                rule_version='ptrade_firstboard_top1.py',candidates=candidates,
                next_open_rule='开盘缺口[0%,6%]；缺口升序、信号日成交额降序、代码升序；最多新买1只；次日流通市值10—200亿元、停牌及涨跌停另核验',
                selection_note='盘后条件沿用PTrade版本：连续6根有成交量；成交额3—19亿元；(近5日最高-近5日最低)/第6根收盘≤20%；可用日线跨度≥50自然日。')
    out=Path('reports')/f'{day}-firstboard-lowgap.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
