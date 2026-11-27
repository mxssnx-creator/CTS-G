#include <algorithm>
#include <cstdint>
#include <vector>

// Return the exit bar of a fixed TP/SL position entered at a closed candle.
// An unfinished position stays open and its unrealized mark is separate.
extern "C" void exits(const double* candles, int bars, double take_profit, double stop_loss,
                      int hold, int direction, int* closed, double* gross) {
    for (int entry = 0; entry < bars; ++entry) {
        const double price = candles[entry * 5 + 3];
        const double stop = price * (1.0 - direction * stop_loss);
        const double target = price * (1.0 + direction * take_profit);
        const int deadline = entry + hold;
        closed[entry] = bars;
        const double* last = candles + (bars - 1) * 5;
        gross[entry] = (last[3] - price) / price * direction;
        for (int i = entry + 1; i < bars && i <= deadline; ++i) {
            const double* bar = candles + i * 5;
            double fill;
            if (direction > 0 && bar[2] <= stop)
                fill = std::min(stop, bar[0]);
            else if (direction < 0 && bar[1] >= stop)
                fill = std::max(stop, bar[0]);
            else if ((direction > 0 && bar[1] >= target) || (direction < 0 && bar[2] <= target))
                fill = target;
            else if (i == deadline)
                fill = bar[3];
            else
                continue;
            closed[entry] = i;
            gross[entry] = (fill - price) / price * direction;
            break;
        }
    }
}

// Consume a packed, sorted series of signals for each independent probe.
// Each direction has its own position and cooldown. Aggregate by exit hour.
extern "C" void trial(const int* long_exit, const int* short_exit, const double* long_gross,
                      const double* short_gross, const int* signal_offset, const int* signal_index,
                      const std::int8_t* side, int count, int start, int end, int cooldown,
                      double cost, double extra_cost, double* out, int* unclosed) {
    const int hours = (end - start) / 60;
    for (int p = 0; p < count; ++p) {
        int long_ready = start - 1;
        int short_ready = start - 1;
        for (int event = signal_offset[p]; event < signal_offset[p + 1]; ++event) {
            const int i = signal_index[event];
            if (i >= end - 1) break;
            const bool is_long = side[event] > 0;
            int& ready = is_long ? long_ready : short_ready;
            if (i < start || i <= ready) continue;
            const int close = (is_long ? long_exit : short_exit)[i];
            ready = close + cooldown;
            if (close >= end) {
                ++unclosed[p];
                continue;
            }
            const double gross = (is_long ? long_gross : short_gross)[i];
            const double net = gross - cost;
            const double stress = net - extra_cost;
            double* hourly = out + (p * hours + (close - start) / 60) * 5;
            hourly[0] += 1.0;
            hourly[1] += std::max(0.0, net);
            hourly[2] += std::max(0.0, -net);
            hourly[3] += std::max(0.0, stress);
            hourly[4] += std::max(0.0, -stress);
        }
    }
}
