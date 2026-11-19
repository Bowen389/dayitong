import json
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import aggregate_watch

class AggregateRecovery(unittest.TestCase):
    def check_status(self,status,expected):
        with tempfile.TemporaryDirectory() as root,chdir(root):
            Path('daily.py').touch()
            def rebuild(*args,**kwargs):
                Path('reports').mkdir(exist_ok=True)
                Path('reports/2026-11-19.json').write_text(json.dumps({'date':'2026-11-19','status':status}))
                return SimpleNamespace(returncode=0)
            with patch('sys.argv',['aggregate_watch.py','--date','2026-11-19']), \
                 patch.object(aggregate_watch,'signatures',return_value=('new-source',)), \
                 patch.object(aggregate_watch.subprocess,'run',side_effect=rebuild):
                self.assertEqual(aggregate_watch.main(),expected)
    def test_partial_report_is_not_success(self):self.check_status('partial',2)
    def test_all_sources_complete_is_success(self):self.check_status('complete',0)

class FactorFallback(unittest.TestCase):
    def test_tencent_factors_are_cacheable_with_native_boolean(self):
        import pandas as pd
        import akshare as ak
        import daily
        dates=pd.bdate_range(end='2026-11-19',periods=25)
        frame=pd.DataFrame({'date':dates,'open':10.,'close':11.,'high':11.,'low':9.,'volume':1000.,'amount':10000.,'turnover':.10})
        fields=['']*49
        fields[3]='11';fields[30]='20261119150000';fields[47]='11'
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return ('v_sz000889="'+'~'.join(fields)+'";').encode()
        with tempfile.TemporaryDirectory() as root,chdir(root), \
             patch.object(ak,'stock_zh_a_hist_tx',return_value=frame), \
             patch.object(daily,'urlopen',return_value=Response()):
            result=daily.top5_tencent('2026-11-19','000889')
            self.assertIs(result['closed_at_limit'],True)
            cached=json.loads(Path('data/factors/2026-11-19-000889.json').read_text())
            self.assertEqual(cached['date'],'2026-11-19')

if __name__=='__main__':unittest.main()
