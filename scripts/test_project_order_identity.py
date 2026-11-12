import sys,unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parent/"regression"),str(Path(__file__).resolve().parents[1]/"server/pulse")]
import pulse_trader as mod

class ProjectIds(unittest.TestCase):
    def test_other_projects_never_belong_to_g(self):
        trader=mod.Pulse.__new__(mod.Pulse)
        self.assertTrue(trader.cid_ours(mod.TAG+"entry123"))
        for identity in ["CTSAV2_E1","CTSBV2_E1","CTSXV2_E1","ctsc_abcd","knabc123entry","ktabc123mentry"]:
            self.assertFalse(trader.cid_ours(identity),identity)

if __name__=="__main__":unittest.main()
