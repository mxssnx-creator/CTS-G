"""A lazy signal × eligible-Set view for the cooperative entry scheduler."""
from bisect import bisect_right


class EntryMatrix:
    """Store references per pack/side, never millions of candidate tuples."""
    def __init__(self, signals, sets_by_scope):
        self.signals = []
        self.ends = []
        self.sets_by_scope = sets_by_scope
        self.phases = []
        for signal in sorted(signals, key=lambda r: (r[1], r[2], r[3])):
            scope = self.scope(signal)
            count = len(sets_by_scope.get(scope, ()))
            if count:
                self.signals.append(signal)
        # Round-robin signals as well as Sets: a huge catalog on the first
        # symbol must not consume every slice before the next symbol is seen.
        counts = [len(sets_by_scope[self.scope(row)]) for row in self.signals]
        start = total = 0
        for end in sorted(set(counts)):
            active = [i for i, count in enumerate(counts) if count > start]
            self.phases.append((start, active))
            total += (end - start) * len(active)
            self.ends.append(total)
            start = end

    @staticmethod
    def scope(signal):
        return ('indications' if signal[3].startswith('ind:') else 'general',
                'LONG' if signal[2] > 0 else 'SHORT')

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __getitem__(self, index):
        if index < 0 or index >= len(self):
            raise IndexError(index)
        phase = bisect_right(self.ends, index)
        round_start, active = self.phases[phase]
        offset = index - (self.ends[phase - 1] if phase else 0)
        round_index, signal_offset = divmod(offset, len(active))
        lane = active[signal_offset]
        signal = self.signals[lane]
        states = self.sets_by_scope[self.scope(signal)]
        selected = (round_start + round_index + lane) % len(states)
        return (*signal, states[selected])
