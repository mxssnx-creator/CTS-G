"""A lazy signal × eligible-Set view for the cooperative entry scheduler."""
from bisect import bisect_right


class EntryMatrix:
    """Store references per pack/side, never millions of candidate tuples."""
    def __init__(self, signals, sets_by_scope):
        self.signals = []
        self.ends = []
        self.sets_by_scope = sets_by_scope
        total = 0
        for signal in sorted(signals, key=lambda r: (r[1], r[2], r[3])):
            scope = self.scope(signal)
            count = len(sets_by_scope.get(scope, ()))
            if count:
                self.signals.append(signal)
                total += count
                self.ends.append(total)

    @staticmethod
    def scope(signal):
        return ('indications' if signal[3].startswith('ind:') else 'general',
                'LONG' if signal[2] > 0 else 'SHORT')

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __getitem__(self, index):
        if index < 0 or index >= len(self):
            raise IndexError(index)
        lane = bisect_right(self.ends, index)
        signal = self.signals[lane]
        start = self.ends[lane - 1] if lane else 0
        return (*signal, self.sets_by_scope[self.scope(signal)][index - start])
