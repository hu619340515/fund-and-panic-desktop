"""历史估计不污染正式值，且逐日回算不读取未来。"""
try:
    import _bootstrap
except ModuleNotFoundError:
    from . import _bootstrap

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from scripts.a_share_panic_index.pipeline.historical import HistoricalService, estimate_records, history_inputs_worker
from scripts.a_share_panic_index.providers.base import ProviderError
from scripts.a_share_panic_index.features.daily import build_daily_feature_values
from tests.helpers import make_database, settings, test_logger


def inputs():
    rows, amounts = [], []
    day = date(2025, 9, 1)
    for i in range(110):
        current = day + timedelta(days=i)
        if current.weekday() > 4:
            continue
        close = 4000 + i * 2 + (i % 7) * 5
        rows.append({'date':current.isoformat(),'open':close-3,'close':close,'high':close+8,'low':close-9})
        amounts.append({'date':current.isoformat(),'amount':1e12+i*1e9})
    return {'index':rows,'amounts':amounts,'qvix':{}}


class HistoricalEstimatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = make_database(Path(self.temp.name))
        self.settings = settings()
        self.logger = test_logger()

    def tearDown(self):
        self.temp.cleanup()

    def test_estimates_are_partial_and_do_not_use_future(self):
        data = inputs()
        cutoff = date(2025, 11, 1)
        before = estimate_records(self.settings,self.database,self.logger,data,cutoff)
        after = estimate_records(self.settings,self.database,self.logger,data,date(2025,12,31))
        self.assertGreater(len(before), 10)
        self.assertEqual(before,[row for row in after if row['trade_date'] < cutoff.isoformat()])
        self.assertTrue(all(row['finality']=='estimated' for row in before))
        self.assertTrue(all(0 < row['coverage'] < 1 for row in before))
        self.assertTrue(all('decline_share' in row['missing_features'] for row in before))
        self.assertEqual(self.database.daily_history(), [])

    def test_missing_actual_amount_date_stays_missing(self):
        data = inputs()
        missing = data['amounts'].pop(45)['date']
        records = estimate_records(self.settings,self.database,self.logger,data,date(2025,12,31))
        self.assertNotIn(missing,[row['trade_date'] for row in records])

    def test_separate_storage_and_failed_download_preserve_history(self):
        service = HistoricalService(self.settings,self.database,self.logger)
        with patch('scripts.a_share_panic_index.pipeline.historical.run_with_hard_timeout',side_effect=[inputs(),{},{}]):
            result = service.refresh(date(2025,12,31))
        self.assertGreater(len(result['records']), 20)
        self.assertEqual(self.database.daily_history(), [])
        with patch('scripts.a_share_panic_index.pipeline.historical.run_with_hard_timeout',side_effect=ProviderError('网络不可用')):
            with self.assertRaises(ProviderError):
                service.refresh(date(2026,1,1))
        failed = service.read()
        self.assertEqual(failed['records'], result['records'])
        self.assertEqual(failed['status']['updated_at'], result['status']['updated_at'])
        self.assertEqual(failed['status']['state'], 'error')
        self.assertIn('网络不可用', failed['status']['message'])
        self.assertEqual(HistoricalService(self.settings,self.database,self.logger).read(), failed)

    def test_missing_market_source_reports_actual_failure(self):
        with patch('scripts.a_share_panic_index.providers.history.fetch_index_history', return_value=inputs()['index']), patch(
            'scripts.a_share_panic_index.providers.history.fetch_market_amount_history_result',
            return_value={'available':False,'rows':[],'error':'上海A股: 连接被关闭'},
        ):
            with self.assertRaisesRegex(ProviderError, '上海A股: 连接被关闭'):
                history_inputs_worker('2026-09-11')

    def test_partial_breadth_and_named_contracts_raise_coverage_without_inventing_thresholds(self):
        data = inputs()
        cutoff = date(2025,12,31)
        original = estimate_records(self.settings,self.database,self.logger,data,cutoff)
        data['breadth'] = {row['date']:{'up_count':1000,'down_count':1900,'flat_count':100,
            'valid_stock_count':3000,'limit_up':20,'limit_down':30} for row in data['index']}
        data['futures'] = {row['date']:[{'symbol':'IF2603','last':3900}, {'symbol':'IF2606','last':3850}]
            for row in data['index']}
        result = estimate_records(self.settings,self.database,self.logger,data,cutoff)
        self.assertGreater(result[-1]['coverage'], original[-1]['coverage'])
        raw = result[-1]['raw_inputs']
        self.assertEqual(raw['front_contract'],'IF2603')
        self.assertIsNotNone(raw['front_annualized_basis'])
        self.assertIn('median_return_stress', result[-1]['missing_features'])
        self.assertIn('severe_decline_share', result[-1]['missing_features'])
        self.assertNotIn('decline_share', result[-1]['missing_features'])
        formal_features = build_daily_feature_values(raw, [])
        self.assertIsNone(formal_features['decline_share'], '正式收盘仍要求完整宽度，不启用历史部分口径')
        missing = data['index'][-2]['date']
        data['breadth'].pop(missing)
        data['futures'].pop(missing)
        partial = estimate_records(self.settings,self.database,self.logger,data,cutoff)
        gap = next(row for row in partial if row['trade_date']==missing)
        self.assertIn('decline_share',gap['missing_features'])
        self.assertIn('front_annualized_basis',gap['missing_features'])
        self.assertEqual(self.database.daily_history(),[])
