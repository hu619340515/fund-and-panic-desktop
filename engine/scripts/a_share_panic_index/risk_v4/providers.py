"""真实日线和辅助观测适配器：保留来源、原响应及日期，禁止拼接估计值。"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .store import timestamp

SYMBOLS = {
    "sh000300":{"name":"沪深300","secid":"1.000300","csi_code":"000300","csi_name":"CSI 300","calendar":"XSHG","start":"2010-01-01"},
    "sh000905":{"name":"中证500","secid":"1.000905","csi_code":"000905","csi_name":"CSI 500","calendar":"XSHG","start":"2010-01-01"},
    "sh000852":{"name":"中证1000","secid":"1.000852","csi_code":"000852","csi_name":"CSI 1000","calendar":"XSHG","start":"2014-10-17"},
    "hkHSI":{"name":"恒生指数","secid":"100.HSI","calendar":"XHKG","start":"2010-01-01"},
    "hkHSTECH":{"name":"恒生科技","secid":"100.HSTECH","calendar":"XHKG","start":"2020-07-27"},
    "cn399808":{"name":"中证新能源","secid":"0.399808","sina_symbol":"sz399808","csi_code":"399808","csi_name":"CSI New Energy","calendar":"XSHG","start":"2015-02-10"},
    "cn399989":{"name":"中证医疗","secid":"0.399989","sina_symbol":"sz399989","csi_code":"399989","csi_name":"CSI Medical Service","calendar":"XSHG","start":"2014-10-31"},
    "cn931865":{"name":"中证半导体产业","secid":"2.931865","sina_symbol":"sh931865","csi_code":"931865","csi_name":"CSI Semiconductor","calendar":"XSHG","start":"2019-03-20"},
    "cn399983":{"name":"沪深300地产等权重","secid":"0.399983","sina_symbol":"sz399983","csi_code":"399983","csi_name":"300 Real Estate EW","calendar":"XSHG","start":"2013-11-22"},
    "cn399975":{"name":"中证全指证券公司","secid":"0.399975","sina_symbol":"sz399975","csi_code":"399975","csi_name":"All Share Brokerage","calendar":"XSHG","start":"2013-07-15"},
    "cn931151":{"name":"中证光伏产业","secid":"2.931151","sina_symbol":"sh931151","csi_code":"931151","csi_name":"Photovoltaic Industry","calendar":"XSHG","start":"2019-04-22"},
    "sh000913":{"name":"沪深300医药卫生","secid":"1.000913","csi_code":"000913","csi_name":"CSI 300 Health Care","calendar":"XSHG","start":"2010-01-01"},
    "cn930707":{"name":"中证畜牧养殖","secid":"2.930707","sina_symbol":"sh930707","csi_code":"930707","csi_name":"CSI Livestock Breeding","calendar":"XSHG","start":"2015-07-13"},
    "cn399997":{"name":"中证白酒","secid":"0.399997","sina_symbol":"sz399997","csi_code":"399997","csi_name":"CSI Liquor","calendar":"XSHG","start":"2015-01-21"},
    "sz980092":{"name":"国证自由现金流","secid":"0.980092","sina_symbol":"sz980092","cni_code":"980092","cni_name":"自由现金流","cni_ename":"CNIFCF","calendar":"XSHG","start":"2012-12-28"},
    "hkHSIII":{"name":"恒生互联网科技业","secid":"100.HSIII","calendar":"XHKG","start":"2015-08-17"},
    "hkHSHYLV":{"name":"恒生港股通高股息低波动","secid":"100.HSHYLV","calendar":"XHKG","start":"2017-05-08"},
}
CORE_SYMBOLS = ("sh000300","sh000905","sh000852")

# HKEX 官方全天停市公告；exchange_calendars 的 XHKG 版本未包含这两次临时停市。
HKEX_EXCEPTIONAL_CLOSURES = {
    "2023-09-01": "https://www.hkex.com.hk/News/Market-Communications/2023/2309012news?sc_lang=en",
    "2023-09-08": "https://www.hkex.com.hk/News/Market-Communications/2023/2309083news?sc_lang=en",
}
EXACT_BENCHMARKS = {
    "中证新能源指数": "cn399808",
    "中证医疗指数": "cn399989",
    "中证半导体产业指数": "cn931865",
    "沪深300地产等权重指数": "cn399983",
    "中证全指证券公司指数": "cn399975",
    "中证光伏产业指数": "cn931151",
    "沪深300医药卫生指数": "sh000913",
    "中证畜牧养殖指数": "cn930707",
    "中证白酒指数": "cn399997",
    "国证自由现金流指数": "sz980092",
    "恒生互联网科技业指数": "hkHSIII",
    "恒生港股通高股息低波动指数": "hkHSHYLV",
    "恒生港股通红利低波动指数": "hkHSHYLV",
}
INDEX_REFERENCES = {
    "cn399808": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/399808factsheet.pdf",
    "cn399989": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/399989factsheet.pdf",
    "cn931865": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/931865factsheet.pdf",
    "cn399983": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/321_399983_Index_Methodology_cn.pdf",
    "cn399975": "https://www.csindex.com.cn/zh-CN/indices/index-detail/399975",
    "cn931151": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/931151factsheet.pdf",
    "sh000913": "https://www.csindex.com.cn/zh-CN/indices/index-detail/000913",
    "cn930707": "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/930707factsheet.pdf",
    "cn399997": "https://www.csindex.com.cn/zh-CN/indices/index-detail/399997",
    "sz980092": "https://www.cnindex.com.cn/html2pdf/preview/jj_980092.pdf",
    "hkHSIII": "https://www.hsi.com.hk/static/uploads/contents/zh_cn/news/pressRelease/20210208T000000.pdf",
    "hkHSHYLV": "https://www.hsi.com.hk/static/uploads/contents/zh_cn/dl_centre/methodologies/IM_hshylvc.pdf",
}
CSI_PERFORMANCE_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
CNI_DAILY_URL = "https://hq.cnindex.com.cn/market/market/getIndexDailyData"
AUXILIARY_UPSTREAMS = {
    "qvix_300_index": "AKShare 的 opttbbs 300股指QVIX公开源",
    "qvix_300_etf": "AKShare 的 opttbbs 300ETF QVIX公开源（指标口径不同）",
    "sina_futures": "新浪财经公开的中金所 IF 合约报价",
    "akshare_futures": "AKShare 免费 IF 行情适配器（上游由 AKShare 运行时决定）",
    "mootdx": "mootdx 行情服务器",
    "eastmoney": "东方财富公开行情接口",
    "tencent": "腾讯公开报价；A股代码表依赖东方财富，非完整独立链路",
    "sina": "新浪财经公开 A 股行情接口",
    "xuangubao": "选股宝公开涨跌停池接口",
}


def resolve_exact_benchmark(benchmark_name):
    """只接受元数据提供的精确全称，避免把行业词或主动基金基准臆测成指数。"""
    name = str(benchmark_name or "").strip()
    symbol = EXACT_BENCHMARKS.get(name)
    if symbol is None:
        return {"verified": False, "symbol": None, "reason": "基准名称未精确匹配已登记的行业指数"}
    return {
        "verified": True,
        "symbol": symbol,
        "name": SYMBOLS[symbol]["name"],
        "source": "csindex_methodology",
        "source_url": INDEX_REFERENCES[symbol],
    }


def resolve_unique_fund_benchmark(fund_type, benchmark, investment_text):
    """仅标准指数基金或 ETF 联接基金的唯一明确目标可自动登记。"""
    benchmark_text = str(benchmark or "")
    candidates = [name for name in EXACT_BENCHMARKS if name in benchmark_text]
    is_index_fund = "标准指数" in str(fund_type or "")
    text = str(investment_text or "")
    is_link = "ETF" in text and "联接" in text
    tracks = any(token in str(investment_text or "") for token in ("跟踪", "标的指数", "完全复制", "目标ETF"))
    if len(candidates) != 1 or not (is_index_fund or is_link) or not tracks:
        return {"verified": False, "symbol": None, "reason": "未同时满足标准指数/ETF联接、唯一基准名称和明确跟踪目标"}
    result = resolve_exact_benchmark(candidates[0])
    result["mapping_basis"] = "public_current_reference"
    result["pit_verified"] = False
    return result


def _calendar(symbol, start, end):
    """创建独立交易日历，避免 exchange_calendars 按名称缓存了较短范围。"""
    import pandas as pd

    name = SYMBOLS[symbol]["calendar"]
    if name == "XSHG":
        from exchange_calendars.exchange_calendar_xshg import XSHGExchangeCalendar

        calendar_type = XSHGExchangeCalendar
    elif name == "XHKG":
        from exchange_calendars.exchange_calendar_xhkg import XHKGExchangeCalendar

        calendar_type = XHKGExchangeCalendar
    else:  # SYMBOLS 是受控表，此处只防止新增错误配置静默通过。
        raise ValueError(f"未支持的交易日历：{name}")
    # 第四版只取 2010 年以来数据；请求首日若是元旦等非交易日，不能传给
    # sessions_in_range 作为边界，也不能把上游误发的该日记录当成交易日。
    start_stamp = max(pd.Timestamp(start), pd.Timestamp("2010-01-04"))
    end_stamp = pd.Timestamp(end)
    # exchange_calendars 不接受单日范围；日线增量刷新合法且应可校验。
    if end_stamp <= start_stamp:
        end_stamp = start_stamp + pd.Timedelta(days=1)
    return calendar_type(start=start_stamp, end=end_stamp)


def expected_close_day(symbol, now=None):
    import pandas as pd

    current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    # 范围覆盖整个自然年；若今日还未收盘，只能给出此前已核实收盘日。
    cal = _calendar(symbol, "2010-01-01", f"{current.year}-12-31")
    day = pd.Timestamp(current.date())
    if not cal.is_session(day):
        return cal.date_to_session(day,direction="previous").date().isoformat()
    close = cal.session_close(day).to_pydatetime().astimezone(ZoneInfo("Asia/Shanghai"))
    return (cal.previous_session(day).date() if current < close+timedelta(minutes=10) else current.date()).isoformat()


def normalize_rows(rows, symbol, start, end):
    import pandas as pd

    normalized = {}
    rejected = []
    rounded_ohlc = []
    non_session_rows = []
    close_only_dates = []
    incomplete_ohlc_dates = []
    cal = _calendar(symbol, start, end)
    for item in rows:
        try:
            day = date.fromisoformat(str(item.get("trade_date",item.get("date")))[:10]).isoformat()
            if not start<=day<=end:
                continue
            day_stamp = pd.Timestamp(day)
            if day_stamp < cal.first_session or day_stamp > cal.last_session or not cal.is_session(day_stamp):
                non_session_rows.append(day)
                continue
            close = float(item["close"])
            numbers = {"close": close}
            for key in ("open", "high", "low"):
                raw = item.get(key)
                numbers[key] = None if raw is None or str(raw).strip() == "" else float(raw)
            # 新浪港股解码会暴露 IEEE 尾差（如 4550.87988 vs 4550.88）。原始
            # payload 不变，按指数两位报价精度规范化后再做OHLC关系校验。
            if symbol.startswith("hk"):
                adjusted = {key: round(value, 2) if value is not None else None for key, value in numbers.items()}
                if adjusted != numbers:
                    rounded_ohlc.append(day)
                numbers = adjusted
            if not math.isfinite(numbers["close"]) or numbers["close"] <= 0:
                raise ValueError("价格非法")
            if any(value is not None and (not math.isfinite(value) or value <= 0) for value in numbers.values()):
                raise ValueError("价格非法")
            comparable_lows = [numbers["close"]] + [numbers[key] for key in ("open",) if numbers[key] is not None]
            comparable_highs = [numbers["close"]] + [numbers[key] for key in ("open",) if numbers[key] is not None]
            if (numbers["low"] is not None and numbers["low"] > min(comparable_lows)) or (numbers["high"] is not None and numbers["high"] < max(comparable_highs)):
                raise ValueError("OHLC关系非法")
            if all(numbers[key] is None for key in ("open", "high", "low")):
                close_only_dates.append(day)
            elif any(numbers[key] is None for key in ("open", "high", "low")):
                incomplete_ohlc_dates.append(day)
            amount = item.get("amount")
            amount = float(amount) if amount is not None and str(amount)!="" else None
            if amount is not None and (not math.isfinite(amount) or amount<0):
                amount = None
            value = dict(numbers,trade_date=day,amount=amount,published_at=None,pit_verified=False,symbol=symbol)
            if day in normalized and normalized[day]!=value:
                raise ValueError("同日存在冲突记录")
            normalized[day]=value
        except (KeyError,TypeError,ValueError) as error:
            rejected.append({"date":str(item.get("trade_date",item.get("date",""))),"error":str(error)})
    if not normalized:
        raise ValueError("来源未返回有效日线")
    records=[normalized[key] for key in sorted(normalized)]
    expected_start = max(pd.Timestamp(records[0]["trade_date"]), cal.first_session)
    expected_end = min(pd.Timestamp(end), cal.last_session)
    expected=[v.date().isoformat() for v in cal.sessions_in_range(expected_start, expected_end)]
    exceptional = []
    if symbol.startswith("hk"):
        exceptional = [day for day in expected if day in HKEX_EXCEPTIONAL_CLOSURES]
        expected = [day for day in expected if day not in HKEX_EXCEPTIONAL_CLOSURES]
    missing=[day for day in expected if day not in normalized]
    positions={day:i for i,day in enumerate(expected)}
    previous=None
    for record in records:
        current=positions.get(record["trade_date"])
        record["gap_before"] = previous is not None and (current is None or current!=previous+1)
        previous=current
    return records,{"missing_dates":missing,"rejected":rejected,"non_session_rows":non_session_rows,"close_only_dates":close_only_dates,"incomplete_ohlc_dates":incomplete_ohlc_dates,"ohlc_rounding_repaired_dates":rounded_ohlc,"exceptional_closures":{day:HKEX_EXCEPTIONAL_CLOSURES[day] for day in exceptional},"publication_verified":False,"notice":"历史日线真实可读，但未取得当时发布时间凭证；可用于描述性回算，不能冒充严格时间外验证样本。"}


def _fetch_csindex_history(symbol, meta, start, end):
    """读取中证官方日频表现；`tradingValue` 官方口径为亿元，转换为元。"""
    if "csi_code" not in meta:
        raise ValueError("该标的没有中证官方日频来源")
    response = requests.get(
        CSI_PERFORMANCE_URL,
        params={
            "indexCode": meta["csi_code"],
            "startDate": start.replace("-", ""),
            "endDate": end.replace("-", ""),
        },
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"},
        timeout=(5, 15),
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("code") != "200" or not isinstance(rows, list) or not rows:
        raise ValueError("中证指数日频响应为空")
    converted = []
    amount_missing_dates = []
    for row in rows:
        code = str(row.get("indexCode", "")).zfill(6)
        name = str(row.get("indexNameEn", "")).strip()
        if code != meta["csi_code"] or name != meta["csi_name"]:
            raise ValueError(f"中证指数标的校验失败：{code} {name}")
        trading_value = row.get("tradingValue")
        try:
            amount = round(float(trading_value) * 100_000_000.0, 2)
        except (TypeError, ValueError):
            amount = None
        if amount is None or not math.isfinite(amount) or amount <= 0:
            amount = None
            amount_missing_dates.append(str(row.get("tradeDate", "")))
        converted.append(
            {
                "trade_date": row.get("tradeDate"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "amount": amount,
            }
        )
    records, quality = normalize_rows(converted, symbol, start, end)
    quality["amount_verified"] = not amount_missing_dates
    quality["amount_source_unit"] = "亿元"
    quality["amount_missing_dates"] = amount_missing_dates
    return {
        "symbol": symbol,
        "name": meta["name"],
        "rows": records,
        "source_id": "csindex",
        "source_url": CSI_PERFORMANCE_URL,
        "fetched_at": timestamp(),
        "amount_unit": "CNY",
        "quality": quality,
        "raw_payload": payload,
    }


def _cnindex_trade_date(value):
    """国证接口时间戳为 UTC 毫秒，按上海时区得到实际交易日。"""
    try:
        milliseconds = float(value)
        if not math.isfinite(milliseconds) or milliseconds <= 0:
            raise ValueError("时间戳非法")
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError) as error:
        raise ValueError(f"国证时间戳非法：{value}") from error


def _fetch_cnindex_history(symbol, meta, start, end):
    """读取国证官网公开日线；成交额单位尚无前端口径凭证，保持为空。"""
    if "cni_code" not in meta:
        raise ValueError("该标的没有国证官方日频来源")
    response = requests.get(
        CNI_DAILY_URL,
        params={"indexCode": meta["cni_code"], "startDate": start, "endDate": end},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://www.cnindex.com.cn",
            "Referer": "https://www.cnindex.com.cn/",
        },
        timeout=(5, 15),
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError("国证指数日频响应为空")
    code = str(data.get("indexCode", "")).zfill(6)
    name = str(data.get("indexName", "")).strip()
    english_name = str(data.get("indexEName", "")).strip()
    if (code, name, english_name) != (meta["cni_code"], meta["cni_name"], meta["cni_ename"]):
        raise ValueError(f"国证指数标的校验失败：{code} {name} {english_name}")
    fields = data.get("item")
    values = data.get("data")
    if not isinstance(fields, list) or not isinstance(values, list) or not values:
        raise ValueError("国证指数日频数据为空")
    required = {"timestamp", "open", "high", "low", "close"}
    if not required.issubset(set(fields)):
        raise ValueError("国证指数日频字段不完整")
    converted = []
    amount_unverified_dates = []
    for raw in values:
        if not isinstance(raw, list) or len(raw) != len(fields):
            raise ValueError("国证指数日频行结构不匹配")
        row = dict(zip(fields, raw))
        trade_date = _cnindex_trade_date(row.get("timestamp"))
        if row.get("amount") is not None and str(row.get("amount")).strip() != "":
            amount_unverified_dates.append(trade_date)
        converted.append({
            "trade_date": trade_date, "open": row.get("open"), "high": row.get("high"),
            "low": row.get("low"), "close": row.get("close"), "amount": None,
        })
    records, quality = normalize_rows(converted, symbol, start, end)
    quality["amount_verified"] = False
    quality["amount_source_unit"] = "未核实"
    quality["amount_unverified_dates"] = amount_unverified_dates
    quality["notice"] += " 国证官网成交额字段缺少已核验的前端单位口径，成交额保持缺失。"
    return {
        "symbol": symbol, "name": meta["name"], "rows": records, "source_id": "cnindex",
        "source_url": CNI_DAILY_URL, "fetched_at": timestamp(), "amount_unit": None,
        "quality": quality, "raw_payload": payload,
    }


def fetch_history(symbol, start=None, end=None):
    if symbol not in SYMBOLS:
        raise ValueError("未登记或未核验的指数代码")
    meta=SYMBOLS[symbol]
    start=max(start or meta["start"],meta["start"])
    end=end or expected_close_day(symbol)
    attempts=[]
    # 国证自由现金流的官网日线可覆盖发布日；优先于可能只有短史的通用行情源。
    if "cni_code" in meta:
        try:
            result = _fetch_cnindex_history(symbol, meta, start, end)
            result["attempts"] = attempts
            return result
        except Exception as error:  # noqa: BLE001 - 国证官网异常后才切换至其它来源
            attempts.append({"source": "cnindex", "error": str(error)})
    url="https://push2his.eastmoney.com/api/qt/stock/kline/get"
    try:
        response=requests.get(url,params={"secid":meta["secid"],"klt":101,"fqt":0,"beg":start.replace("-",""),"end":end.replace("-",""),"lmt":10000,"fields1":"f1,f2,f3,f4,f5,f6","fields2":"f51,f52,f53,f54,f55,f56,f57"},headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"},timeout=(5,15))
        response.raise_for_status()
        payload=response.json();data=payload.get("data")
        if not isinstance(data,dict) or payload.get("rc")!=0:
            raise ValueError("日线响应为空")
        expected_code=meta["secid"].split(".",1)[1]
        if str(data.get("code"))!=expected_code:
            raise ValueError("来源返回的标的与请求不一致")
        rows=[]
        for raw in data.get("klines",[]):
            fields=raw.split(",")
            if len(fields)<7:continue
            rows.append(dict(zip(("trade_date","open","close","high","low","volume","amount"),fields[:7])))
        records,quality=normalize_rows(rows,symbol,start,end)
        return {"symbol":symbol,"name":meta["name"],"rows":records,"source_id":"eastmoney","source_url":url,"fetched_at":timestamp(),"amount_unit":"HKD" if symbol.startswith("hk") else "CNY","quality":quality,"raw_payload":payload,"attempts":attempts}
    except Exception as error:  # noqa: BLE001 - 为尝试独立备用来源保留失败证据
        attempts.append({"source":"eastmoney","error":str(error)})
    # 中证官方接口独立于东方财富，并明确给出成交金额（亿元）；只供中证/A 股指数使用。
    if "csi_code" in meta:
        try:
            result = _fetch_csindex_history(symbol, meta, start, end)
            result["attempts"] = attempts
            return result
        except Exception as error:  # noqa: BLE001 - 继续保留无成交额但可核验的新浪日线
            attempts.append({"source":"csindex","error":str(error)})
    # 新浪有独立上游；不把 AKShare 对东方财富的另一封装当交叉验证。
    try:
        import py_mini_racer
        from akshare.index.cons import zh_sina_index_stock_hist_url
        from akshare.stock.cons import hk_js_decode
        sina_code = meta.get("sina_symbol", symbol)
        sina_url = ("https://finance.sina.com.cn/stock/hkstock/"+symbol[2:]+"/klc2_kl.js") if symbol.startswith("hk") else zh_sina_index_stock_hist_url.format(sina_code)
        raw_response=requests.get(sina_url,headers={"User-Agent":"Mozilla/5.0"},timeout=(5,15))
        raw_response.raise_for_status()
        decoder=py_mini_racer.MiniRacer();decoder.eval(hk_js_decode)
        raw=decoder.call("d",raw_response.text.split("=",1)[1].split(";",1)[0].replace('"',''))
        # AKShare 解码的 amount 字段未提供可核验的币种和金额单位；即使数值看似
        # 合理（港股亦然）也不能输入成交额/过热特征。原始 raw 响应仍完整保留。
        dropped_amount_dates = [str(item.get("date", "")) for item in raw if item.get("amount") is not None]
        usable_rows = [{**item, "amount": None} for item in raw]
        records,quality=normalize_rows(usable_rows,symbol,start,end)
        quality["amount_verified"] = False
        quality["amount_unverified_dates"] = dropped_amount_dates
        quality["notice"]+=" 新浪备用日线不提供可核验成交金额单位，过热成交项保持缺失。"
        return {"symbol":symbol,"name":meta["name"],"rows":records,"source_id":"sina","source_url":sina_url,"fetched_at":timestamp(),"amount_unit":None,"quality":quality,"raw_payload":{"response_text":raw_response.text,"decoder":"akshare.stock.cons.hk_js_decode"},"attempts":attempts}
    except Exception as error:  # noqa: BLE001 - 备用来源失败应显式返回给调用方
        attempts.append({"source":"sina","error":str(error)})
    raise RuntimeError("；".join(a["source"]+"："+a["error"] for a in attempts))


def fetch_intraday():
    response=requests.get("https://qt.gtimg.cn/q="+",".join(CORE_SYMBOLS),headers={"User-Agent":"Mozilla/5.0"},timeout=(4,8))
    response.raise_for_status();response.encoding="gbk"
    rows=[]
    for symbol in CORE_SYMBOLS:
        marker='v_'+symbol+'="'
        if marker not in response.text:continue
        fields=response.text.split(marker,1)[1].split('"',1)[0].split("~")
        try:
            last=float(fields[3]);previous=float(fields[4]);stamp=datetime.strptime(fields[30],"%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            if last<=0 or previous<=0:continue
            rows.append({"symbol":symbol,"name":SYMBOLS[symbol]["name"],"last":last,"change":last/previous-1,"timestamp":stamp.isoformat(),"source_id":"tencent","state":"ready" if abs((datetime.now(ZoneInfo("Asia/Shanghai"))-stamp).total_seconds())<=180 else "stale"})
        except (IndexError,ValueError):continue
    return {"rows":rows,"fetched_at":timestamp(),"source_id":"tencent","raw_payload":response.text,"notice":"盘中仅预警，正式模型只使用完整收盘数据。"}


def _verified_source_timestamp(snapshot):
    flags = list(snapshot.get("quality_flags") or [])
    if "provider_timestamp_unavailable" in flags:
        return None, flags + ["unverified_time"]
    value = snapshot.get("source_timestamp")
    if not value:
        return None, flags + ["unverified_time"]
    return str(value), flags


def _same_trade_date(value, trade_date):
    try:
        return datetime.fromisoformat(str(value)).date().isoformat() == trade_date
    except ValueError:
        return False


def fetch_auxiliary(*, trade_date=None, timeout=20):
    """逐项采集辅助观测；仅切换来源，不从旧分数或相邻日期补值。"""
    from ..providers.live import fetch_live

    day = str(trade_date or expected_close_day("sh000300"))
    context = {"trade_date": day, "timeout": float(timeout), "symbol": "sh000300"}
    requested = {
        "qvix": [("qvix_300_index", "qvix"), ("qvix_300_etf", "qvix")],
        "futures": [("sina_futures", "futures"), ("akshare_futures", "futures"), ("mootdx", "futures")],
        "breadth": [("eastmoney", "breadth"), ("tencent", "breadth"), ("sina", "breadth"), ("mootdx", "breadth")],
        "limits": [("eastmoney", "limits"), ("xuangubao", "limits")],
    }
    values = {}
    attempts = []
    for name, candidates in requested.items():
        errors = []
        for index, (provider, semantic_type) in enumerate(candidates):
            try:
                snapshot = fetch_live(provider, semantic_type, context)
                source_time, flags = _verified_source_timestamp(snapshot)
                # IF 基差只能使用同一交易日且有明确时分秒的报价，不能以抓取时刻替代。
                if name == "futures" and not _same_trade_date(source_time, day):
                    raise ValueError("IF来源没有可核验的同交易日时间戳")
                values[name] = {
                    "state": "ready",
                    "data": snapshot.get("data"),
                    "source_id": snapshot.get("provider", provider),
                    "upstream": AUXILIARY_UPSTREAMS.get(provider, "未登记上游"),
                    "timestamp": source_time,
                    "quality_flags": flags,
                    "snapshot": snapshot,
                    "error": None,
                }
                attempts.append({"name": name, "provider": provider, "semantic_type": semantic_type, "upstream": AUXILIARY_UPSTREAMS.get(provider, "未登记上游"), "ok": True, "fallback": index > 0})
                break
            except Exception as error:  # noqa: BLE001 - 各来源独立失败，不应伪造其他项
                message = str(error)
                errors.append(f"{provider}: {message}")
                attempts.append({"name": name, "provider": provider, "semantic_type": semantic_type, "upstream": AUXILIARY_UPSTREAMS.get(provider, "未登记上游"), "ok": False, "error": message})
        else:
            values[name] = {
                "state": "unavailable", "data": None, "source_id": None, "upstream": None,
                "timestamp": None, "quality_flags": ["unavailable"],
                "snapshot": None, "error": "；".join(errors),
            }
    return {"trade_date": day, "fetched_at": timestamp(), "attempts": attempts, **values}
