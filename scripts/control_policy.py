"""Independent chronological control classification, after execution costs.

Controls describe evidence quality. They never change entry-time Base/Main/Real
gates or reinterpret a losing trade as a win.
"""
from dataclasses import asdict, dataclass
import math

import numpy as np

from position_cost import POSITIVE_PF
from validation_policy import CONTROL_MIN_TRADES, CONTROL_MIN_PF

METRICS = ('n','wins','net','cost','gain','loss','maxDd','maxDdtBars',
           'trainN','trainNet','testN','testNet','disabled','costRSum',
           'trainGain','trainLoss','trainCostRSum','testGain','testLoss','testCostRSum')


@dataclass(frozen=True)
class ControlRule:
    training_n: int = 0
    control_n: int = CONTROL_MIN_TRADES
    training_pf: float = POSITIVE_PF
    control_pf: float = CONTROL_MIN_PF

    def __post_init__(self):
        for n,minimum in ((self.training_n,0),(self.control_n,0)):
            if type(n) is not int or n<minimum:raise ValueError('Additional sample counts must be nonnegative')
        if not math.isfinite(self.training_pf) or self.training_pf<POSITIVE_PF:
            raise ValueError('Training PF cannot undercut the Base minimum')
        if not math.isfinite(self.control_pf) or self.control_pf<1.0:
            raise ValueError('Control PF cannot undercut cost-neutral')

    def settings(self):
        return asdict(self)

    def training_mask(self, metrics):
        m=np.asarray(metrics,dtype=float)
        if m.ndim!=2 or m.shape[1]!=len(METRICS) or not np.all(np.isfinite(m[:,[8,9,16]])):
            raise ValueError('Expected finite training results')
        pf=1+.1*np.divide(m[:,16],m[:,8],out=np.zeros(len(m)),where=m[:,8]>0)
        return ((m[:,8]>0) & (m[:,8]>=self.training_n)) & (m[:,9]>1e-12) & (pf>self.training_pf+1e-9)

    def masks(self, metrics):
        m=np.asarray(metrics,dtype=float)
        if m.ndim!=2 or m.shape[1]!=len(METRICS) or not np.all(np.isfinite(m)):
            raise ValueError('Expected finite 20-column result vectors')
        train_pf=1+.1*np.divide(m[:,16],m[:,8],out=np.zeros(len(m)),where=m[:,8]>0)
        control_pf=1+.1*np.divide(m[:,19],m[:,10],out=np.zeros(len(m)),where=m[:,10]>0)
        sample=(m[:,8]>0) & (m[:,8]>=self.training_n)
        positive=m[:,9]>1e-12
        training=sample & positive & (train_pf>self.training_pf+1e-9)
        complete=m[:,10]>=self.control_n
        control_positive=m[:,11]>1e-12
        control_ok=control_pf>self.control_pf+1e-9
        qualified=training & complete & control_positive & control_ok
        states={
            'training-insufficient':~sample,
            'training-nonpositive':sample & ~positive,
            'training-pf':sample & positive & ~training,
            'control-insufficient':training & ~complete,
            'control-nonpositive':training & complete & ~control_positive,
            'control-pf':training & complete & control_positive & ~control_ok,
            'qualified':qualified,
        }
        if self.control_n == 0:
            qualified=training.copy()
            states={k:v for k,v in states.items() if k.startswith('training-')}
            states['control-disabled']=qualified
        return dict(training=training,qualified=qualified,states=states,
                    controlChecked=self.control_n>0,
                    controlPositive=training & (m[:,10]>0) & control_positive & control_ok,
                    trainPf=train_pf,controlPf=control_pf)

    def classify(self, metric):
        result=self.masks([[metric[k] for k in METRICS]])
        return next(key for key,mask in result['states'].items() if mask[0])


def training_winner(metrics, rule, *, eligible=None):
    """One policy per exact risk Set, selected without using any control field."""
    ids=np.flatnonzero(rule.training_mask(metrics) if eligible is None else eligible)
    return int(ids[np.argmax(np.asarray(metrics)[ids,9])]) if len(ids) else None
