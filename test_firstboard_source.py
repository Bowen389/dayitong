import unittest
import pandas as pd
from firstboard_source import eligible

class Rules(unittest.TestCase):
    def frame(self):
        return pd.DataFrame(dict(date=pd.bdate_range(end='2026-11-19',periods=70),close=10.,high=11.,low=9.,volume=100.,amount=500_000_000.))
    def test_ptrade_amplitude_uses_sixth_close(self):
        frame=self.frame()
        self.assertTrue(eligible(frame))
        frame.loc[frame.index[-1],'high']=11.1
        self.assertFalse(eligible(frame))
    def test_recent_halt_and_missing_session_are_excluded(self):
        frame=self.frame()
        frame.loc[frame.index[-2],'volume']=0
        self.assertFalse(eligible(frame))
        frame=self.frame().drop(68)
        self.assertFalse(eligible(frame))
    def test_short_history_and_amount_bounds(self):
        self.assertFalse(eligible(self.frame().tail(6)))
        frame=self.frame()
        frame.loc[frame.index[-1],'amount']=1_900_000_001.
        self.assertFalse(eligible(frame))

if __name__=='__main__':unittest.main()
