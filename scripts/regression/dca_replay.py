"""Causal DCA replay checks; no exchange access."""
import os
import sys
from common import ROOT
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
from stack_eval import admit_dca_candidates, dca_prior_bar_indication


def c(key, now, close, net, rung=0):
    return {'parent_key': key, 'now': now, 'close': close, 'net': net,
            'symbol': f'S{key:03}', 'rung': rung, 'parent': None}


def later_opened_parents_can_supply_prior_closed_evidence():
    late_hit = c(0, 100, 120, 1)
    losses = [c(i+1, 1+i, 20+i, -1) for i in range(8)]
    admitted = admit_dca_candidates([late_hit]+losses, 1)
    return len(admitted) == 8 and late_hit not in admitted, f'admitted parent keys={[v["parent_key"] for v in admitted]}'


def last_n_is_ordered_by_close_not_parent_entry():
    # All adds are admitted before any closes. The first 10 closes win and
    # the last 10 lose, while their parent-key order is the opposite.
    prior = [c(i, 1, 21-i, -1 if i < 10 else 1) for i in range(20)]
    later = c(20, 30, 40, 1)
    admitted = admit_dca_candidates(prior+[later], 1)
    return len(admitted) == 20 and later not in admitted, f'first 20 admitted={len(admitted)}; later blocked={later not in admitted}'


def simultaneous_closes_do_not_influence_same_clock():
    prior = [c(i, 1, 100, -1) for i in range(8)]
    same, after = c(8, 100, 150, 1), c(9, 101, 150, 1)
    admitted = admit_dca_candidates(prior+[same,after], 1)
    return same in admitted and after not in admitted, f'same-clock accepted={same in admitted}; later blocked={after not in admitted}'


def stopped_parent_does_not_resume_later_rungs():
    losses = [c(i, 1, 20+i, -1) for i in range(8)]
    wins = [c(20+i, 2, 150+i, 2) for i in range(15)]
    first, second = c(40, 100, 300, 1, 0), c(40, 200, 300, 1, 1)
    resumed = admit_dca_candidates(losses+wins+[first,second], 1, stop_parent=False)
    stopped = admit_dca_candidates(losses+wins+[first,second], 1, stop_parent=True)
    return (first not in stopped and second not in stopped and second in resumed), f'without stop second={second in resumed}; with stop second={second in stopped}'


def intrabar_hit_uses_the_prior_completed_bar():
    seen=[]
    def direction(symbol, index, names):
        seen.append(index)
        return 1 if index == 9 else -1
    ok = dca_prior_bar_indication(direction, 'A', 10, 1, ['source'])
    no_prior = dca_prior_bar_indication(direction, 'A', 0, 1, ['source'])
    return ok and not no_prior and seen == [9], f'seen bar indices={seen}; no-prior accepted={no_prior}'


CHECKS = [('dca-replay.'+fn.__name__.replace('_','-'), fn) for fn in (
    later_opened_parents_can_supply_prior_closed_evidence, last_n_is_ordered_by_close_not_parent_entry,
    simultaneous_closes_do_not_influence_same_clock, stopped_parent_does_not_resume_later_rungs,
    intrabar_hit_uses_the_prior_completed_bar)]
