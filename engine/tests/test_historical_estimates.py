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

from scripts.a_share_panic_index.pipeline.historical import HistoricalService, estimate_records
from scripts.a_share_panic_index.providers.base import ProviderError
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
        with patch('scripts.a_share_panic_index.pipeline.historical.run_with_hard_timeout',side_effect=[inputs(),{}]):
            result = service.refresh(date(2025,12,31))
        self.assertGreater(len(result['records']), 20)
        self.assertEqual(self.database.daily_history(), [])
        with patch('scripts.a_share_panic_index.pipeline.historical.run_with_hard_timeout',side_effect=ProviderError('网络不可用')):
            with self.assertRaises(ProviderError):
                service.refresh(date(2026,1,1))
        self.assertEqual(service.read(),result)
