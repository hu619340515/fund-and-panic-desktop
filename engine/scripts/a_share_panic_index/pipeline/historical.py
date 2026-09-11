"""从真实日线回算历史估计，与正式收盘表隔离保存。"""
from __future__ import annotations

import json
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from math import log
from statistics import stdev
from typing import Any

from ..features.daily import build_daily_feature_values
from ..features.derivatives import select_if_contracts, annualized_basis
from ..providers.base import ProviderError, run_with_hard_timeout
from .daily import DailyPipeline


def history_inputs_worker(as_of_text: str) -> dict[str, Any]:
    from ..providers.history import fetch_index_history, fetch_market_amount_history_result
    as_of = date.fromisoformat(as_of_text)
    start = as_of - timedelta(days=500)
    try:
        index = fetch_index_history('sh000300', start, as_of - timedelta(days=1), timeout_seconds=10)
    except ProviderError as error:
        raise ProviderError(f'沪深300历史日线下载失败（东方财富）：{error}') from error
    amounts = fetch_market_amount_history_result(as_of, natural_days=500, timeout_seconds=10)
    if not amounts['available']:
        raise ProviderError(f"沪深A股历史成交额下载失败（东方财富）：{amounts.get('error') or '没有共同交易日'}；请稍后重试")
    return {'index': index, 'amounts': amounts['rows']}


def qvix_history_worker() -> dict[str, float]:
    import akshare as ak
    frame = ak.index_option_300etf_qvix()
    return {
        str(row['date'])[:10]: float(row['close'])
        for _, row in frame.iterrows()
        if row.get('close') is not None and float(row['close']) > 0
    }


def extra_history_worker(days: list[str], as_of_text: str) -> dict:
    from ..providers.history_extra import fetch_extra_history
    return fetch_extra_history(days, date.fromisoformat(as_of_text))


def estimate_records(settings, database, logger, inputs: dict, as_of: date) -> list[dict]:
    """按日期前推；每个日期仅使用其自身及此前的输入，不依赖正式表。"""
    index = sorted((row for row in inputs['index'] if str(row['date']) < as_of.isoformat()), key=lambda row: str(row['date']))
    amounts = {str(row['date']): float(row['amount']) for row in inputs['amounts'] if float(row['amount']) > 0}
    qvix = inputs.get('qvix', {})
    pipeline = DailyPipeline(settings, database, logger)
    raw_history, score_history, output = [], [], []
    try:
        start = as_of.replace(year=as_of.year - 1)
    except ValueError:
        start = as_of.replace(year=as_of.year - 1, day=28)
    for position, row in enumerate(index):
        day = str(row['date'])
        if position < 21 or day not in amounts:
            continue
        previous = index[position - 1]
        # 波动率基线只使用截至前一交易日的20次收益率。
        returns = [log(float(index[j]['close']) / float(index[j-1]['close'])) for j in range(position-20, position)]
        sigma = stdev(returns)
        if sigma <= 0:
            continue
        qvalue = qvix.get(day)
        qprevious = qvix.get(str(previous['date']))
        raw = {
            'trade_date': day,
            **{name: float(row[name]) for name in ('open','high','low','close')},
            'previous_close': float(previous['close']),
            'market_amount': amounts[day], 'daily_sigma': sigma,
            'qvix': qvalue,
            'qvix_daily_change': log(qvalue/qprevious) if qvalue and qprevious else None,
            'sources': {
                'index': {'provider':'eastmoney', 'symbol':'sh000300', 'source_timestamp':day},
                'market_amount': {'provider':'eastmoney', 'symbol':'sh000002+sz399107', 'unit':'CNY', 'source_timestamp':day},
                **({'qvix': {'provider':'qvix_300_etf', 'source_timestamp':day}} if qvalue else {}),
            },
        }
        breadth = inputs.get('breadth', {}).get(day)
        if breadth:
            raw.update({key: breadth[key] for key in ('up_count','down_count','flat_count','valid_stock_count','limit_up','limit_down') if key in breadth})
            raw['sources']['breadth'] = {'provider':'kaipanhong', 'source_timestamp':day,
                'scope':'开盘红全市场家数口径；历史估计使用，未推导分桶阈值或中位数'}
        contracts = inputs.get('futures', {}).get(day, [])
        if contracts:
            try:
                front, next_contract = select_if_contracts(contracts, date.fromisoformat(day),
                    int(settings.get('futures.minimum_days_to_expiry')))
                for name, contract in (('front',front),('next',next_contract)):
                    if contract:
                        raw[f'{name}_annualized_basis'] = annualized_basis(raw['close'],contract['price'],
                            (contract['expiry']-date.fromisoformat(day)).days, int(settings.get('futures.annualization_days')))
                        raw[f'{name}_contract'] = contract['symbol']
                        raw[f'{name}_price'] = contract['price']
                        raw[f'{name}_expiry'] = contract['expiry'].isoformat()
                if raw.get('next_annualized_basis') is not None:
                    raw['basis_curve_stress'] = raw['front_annualized_basis'] - raw['next_annualized_basis']
                if len(raw_history) >= 3 and raw_history[-3].get('front_annualized_basis') is not None:
                    raw['basis_expansion_3d'] = raw['front_annualized_basis']-raw_history[-3]['front_annualized_basis']
                raw['sources']['futures'] = {'provider':'cffex','source_timestamp':day,'unit':'index_points',
                    'price_type':'close','contracts':[raw.get('front_contract'),raw.get('next_contract')]}
            except ValueError:
                pass  # 当日缺少满足既有换月规则的合约，保留缺项。
        values = build_daily_feature_values(raw, raw_history, allow_partial_breadth=True)
        result = pipeline._score(date.fromisoformat(day), raw, values, historical_estimate=True, feature_history=score_history)
        record = result.to_dict()
        record['missing_features'] = [name for name, value in values.items() if value is None]
        record['sources'] = raw['sources']
        record['method'] = '真实日线历史估计；宽度采用开盘红家数口径，缺少精确收益中位数与跌幅阈值不补造，非正式收盘值'
        record['raw_inputs'] = raw
        raw_history.append(raw)
        score_history.append({'feature_scores':result.feature_scores})
        if day >= start.isoformat():
            output.append(record)
    return output


class HistoricalService:
    def __init__(self, settings, database, logger):
        self.settings, self.database, self.logger = settings, database, logger
        self.lock = threading.Lock()
        with closing(database.connect()) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS historical_estimates (trade_date TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS historical_estimate_status (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            connection.commit()

    def read(self) -> dict:
        with closing(self.database.connect()) as connection:
            rows = connection.execute('SELECT payload FROM historical_estimates ORDER BY trade_date').fetchall()
            status = connection.execute('SELECT payload FROM historical_estimate_status WHERE id=1').fetchone()
        return {'records':[json.loads(row[0]) for row in rows], 'status':json.loads(status[0]) if status else {'state':'empty','message':'尚未下载历史行情'}}

    def refresh(self, as_of: date) -> dict:
        if not self.lock.acquire(blocking=False):
            raise ProviderError('历史补全正在进行，请稍后查看')
        try:
            inputs = run_with_hard_timeout(history_inputs_worker, (as_of.isoformat(),), 100)
            errors = []
            try:
                inputs['qvix'] = run_with_hard_timeout(qvix_history_worker, (), 20)
            except ProviderError as error:
                errors.append('QVIX历史缺失：' + str(error))
            try:
                extras = run_with_hard_timeout(extra_history_worker,
                    ([str(row['date']) for row in inputs['index']], as_of.isoformat()), 115)
                inputs.update({key:extras.get(key,{}) for key in ('futures','breadth')})
                errors.extend(extras.get('errors',[]))
            except ProviderError as error:
                errors.append('IF/市场宽度历史缺失：' + str(error))
            records = estimate_records(self.settings, self.database, self.logger, inputs, as_of)
            if not records:
                raise ProviderError('历史行情不足，无法回算；保留已有历史估计')
            status = {'state':'ready','updated_at':datetime.now().astimezone().isoformat(),'errors':errors,
                      'message':f'已回算 {len(records)} 个交易日；历史估计与正式值分开保存',
                      'source_coverage':{name:sum(name in r['sources'] for r in records) for name in ('index','market_amount','qvix','futures','breadth')}}
            with closing(self.database.connect()) as connection:
                with connection:
                    connection.execute('DELETE FROM historical_estimates')
                    connection.executemany('INSERT INTO historical_estimates VALUES (?,?)', [(r['trade_date'],json.dumps(r,ensure_ascii=False,allow_nan=False)) for r in records])
                    connection.execute('INSERT OR REPLACE INTO historical_estimate_status VALUES (1,?)',(json.dumps(status,ensure_ascii=False),))
            return self.read()
        except ProviderError as error:
            status = self.read()['status']
            status.update(state='error', last_attempt_at=datetime.now().astimezone().isoformat(),
                          message=f'历史补全失败：{error}', errors=[str(error)])
            with closing(self.database.connect()) as connection:
                with connection:
                    connection.execute('INSERT OR REPLACE INTO historical_estimate_status VALUES (1,?)',
                                       (json.dumps(status, ensure_ascii=False),))
            self.logger.warning('历史补全失败：%s', error)
            raise
        finally:
            self.lock.release()
