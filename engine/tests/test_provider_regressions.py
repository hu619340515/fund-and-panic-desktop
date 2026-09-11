"""实网发现的截断市场列表与期货字段回归。"""
try:
    import _bootstrap
except ModuleNotFoundError:
    from . import _bootstrap

import unittest
from unittest.mock import Mock, patch
from scripts.a_share_panic_index.providers import live
from scripts.a_share_panic_index.providers.base import ProviderDataError


class ProviderRegressions(unittest.TestCase):
    def test_truncated_market_page_is_not_whole_market(self):
        response = Mock()
        response.json.return_value = {'data': {'total': 5200, 'diff': [
            {'f2': 10, 'f3': 5, 'f6': 100000, 'f12': '600000', 'f13': 1}
        ] * 100}}
        with patch.object(live.HttpClient, 'get', return_value=response):
            for fetch in (live.fetch_eastmoney_breadth, live.fetch_tencent_breadth):
                with self.assertRaises(ProviderDataError):
                    fetch({})

    def test_futures_last_is_not_open_or_high(self):
        fields = ['0'] * 50
        for index, value in {0:'4508.2',1:'4520',2:'4400',3:'4457',16:'4456.8',26:'4457.2',36:'2026-09-11',37:'10:35:30'}.items():
            fields[index] = value
        response = Mock(text='var hq_str_CFF_RE_IF2609="' + ','.join(fields) + '";')
        with patch.object(live.HttpClient, 'get', return_value=response), patch.object(live, '_if_contract_symbols', return_value=['IF2609']):
            result = live.fetch_sina_futures({'trade_date':'2026-09-11'})
        contract = result['data']['contracts'][0]
        self.assertEqual(contract['last'], 4457)
        self.assertEqual(contract['bid'], 4456.8)
        self.assertEqual(contract['ask'], 4457.2)
        self.assertEqual(result['source_timestamp'], '2026-09-11T10:35:30+08:00')
