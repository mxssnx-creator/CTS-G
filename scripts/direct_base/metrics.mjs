// Used unchanged by the exhaustive batch and by the offline report.
export const AXES = ['Off', 'Pause 1', 'Pause 2', 'Prev 4']
export const BLOCKS = [
  {count: 0, ratio: 0, label: 'Off'},
  {count: 1, ratio: 0.1, label: '1 order / 0.10'},
  {count: 1, ratio: 0.2, label: '1 order / 0.20'},
  {count: 2, ratio: 0.1, label: '2 orders / 0.10'},
  {count: 2, ratio: 0.2, label: '2 orders / 0.20'},
]

export function streamId(data, bf, base, ef, end) {
  const parent = data.base[bf][base]
  return end < 0 ? parent : data.ends[parent][ef][end]
}

// Report chunks retain a byte index, so opening one lane never allocates every
// sibling's order list. The batch operates on the original integer arrays.
export function orderIds(data,id) {
  if (data.streams) return data.streams[id]
  const start=data.offsets.getUint32(id*4,true), end=data.offsets.getUint32((id+1)*4,true)
  const ids=[]
  let previous=0, delta=0, factor=1
  for(let i=start;i<end;i++) {
    const byte=data.delta[i]
    delta+=(byte&127)*factor
    if(byte>=128) { factor*=128; continue }
    previous+=delta
    ids.push(previous)
    delta=0
    factor=1
  }
  return ids
}

export function pf(gain, loss) {
  return loss > 0 ? gain / loss : (gain > 0 ? Infinity : null)
}

function recentPF(data, stream, end, n) {
  let gain = 0, loss = 0
  for (let i = Math.max(0, end - n); i < end; i++) {
    const net = data.orders[stream[i]][3]
    if (net > 0) gain += net
    else loss -= net
  }
  return loss ? gain / loss : (gain ? 99 : 0)  // native axis_sets.pf convention
}

export function prepare(data, stream, period) {
  const entries = stream.filter(id => {
    const at = data.orders[id][0]
    return at >= period.warm && at < period.minutes
  })
  // Symbol index is alphabetical. Same-bar closes are invisible to all entries.
  entries.sort((a, b) => data.orders[a][0] - data.orders[b][0]
                       || data.orders[a][2] - data.orders[b][2] || a - b)
  return entries
}

export function axisPlan(data, stream, entries, axis, floor) {
  let closed = 0, streak = 0, previous = -1, previousOK = true
  const orders = data.orders
  const admitted = entries.filter(id => {
    if (!axis) return true
    const at = orders[id][0]
    while (closed < stream.length) {
      const r = orders[stream[closed]]
      if (r[1] === null || r[1] >= at) break
      streak = r[3] < 0 ? streak + 1 : 0
      closed++
    }
    if (axis === 1 || axis === 2) return streak < axis
    if (previous !== closed) {
      previousOK = closed < 8 || recentPF(data, stream, closed, 8) + 1e-9 >= 0.85 * floor
                              || recentPF(data, stream, closed, 50) + 1e-9 >= 0.85 * floor
      previous = closed
    }
    return previousOK
  })
  // The native order IDs are in actual settlement order, including symbol ties.
  const settled = admitted.map((_, i) => i).filter(i => orders[admitted[i]][1] !== null)
                          .sort((a, b) => admitted[a] - admitted[b])
  return {admitted, settled}
}

export function measure(data, plan, period, blockIndex, detail = false) {
  const {admitted, settled} = plan
  const {count, ratio} = BLOCKS[blockIndex]
  const orders = data.orders
  const hourCount = (period.minutes - period.warm) / 60
  const hours = new Float64Array(hourCount)
  const perHour = new Uint32Array(hourCount)
  const levels = new Uint8Array(admitted.length)
  // Each consecutive block owns its members at entry. Concurrent positions
  // can settle out of order. Only completed blocks at the front advance size.
  const blocks = []
  let level = 0, recovery = 0, nextBlock = 0
  let entered = 0, exited = 0, wins = 0, raised = 0
  let gain = 0, loss = 0, gainA = 0, lossA = 0, gainB = 0, lossB = 0, nA = 0
  let equity = 0, peak = 0, drawdown = 0
  while (exited < settled.length || entered < admitted.length) {
    const nextEntry = entered < admitted.length ? orders[admitted[entered]][0] : Infinity
    const nextExit = exited < settled.length ? orders[admitted[settled[exited]]][1] : Infinity
    if (nextEntry <= nextExit) {
      if (count) {
        if (entered % count === 0) blocks.push({closed: 0, net: 0, weighted: 0})
        levels[entered] = level
        if (level) raised++
      }
      entered++
      continue
    }
    let tickNet = 0
    do {
      const index = settled[exited++]
      const r = orders[admitted[index]]
      const net = r[3] * (1 + ratio * levels[index])
      if (count) {
        const group = blocks[Math.floor(index / count)]
        group.closed++
        group.net += r[3]
        group.weighted += net
        while (nextBlock < blocks.length && blocks[nextBlock].closed === count) {
          const done = blocks[nextBlock++]
          if (level === 0) {
            if (done.net < 0) { level = 1; recovery = done.weighted }
          } else {
            recovery += done.weighted
            if (recovery >= 0) { level = 0; recovery = 0 }
            else if (done.net < 0) level = Math.min(2, level + 1)
          }
        }
      }
      const hour = Math.ceil((r[1] - period.warm) / 60) - 1
      hours[hour] += net
      perHour[hour]++
      const g = Math.max(0, net), l = Math.max(0, -net)
      gain += g; loss += l; wins += net > 0
      if (hour < hourCount / 2) { gainA += g; lossA += l; nA++ }
      else { gainB += g; lossB += l }
      tickNet += net
    } while (exited < settled.length && orders[admitted[settled[exited]]][1] === nextExit)
    equity += tickNet
    peak = Math.max(peak, equity)
    drawdown = Math.max(drawdown, peak - equity)
  }
  let positive = 0, active = 0
  for (let h = 0; h < hourCount; h++) {
    if (hours[h] > 0) positive++
    if (perHour[h]) active++
  }
  const result = {n: exited, open: entered - exited, gain, loss, wins, raised, positive, active, drawdown,
                  nA, gainA, lossA, gainB, lossB, hours: hourCount}
  if (detail) {
    result.hourNet = Array.from(hours)
    result.hourN = Array.from(perHour)
    result.trades = admitted.map((id, i) => [id, 1 + levels[i] * ratio])
  }
  return result
}

export function supported(m) {
  return m.n >= 60 && m.active >= Math.ceil(m.hours / 2) && m.nA >= 20 && m.n - m.nA >= 20
}

export function rank(m) {
  return [Math.min(pf(m.gainA, m.lossA) ?? 0, pf(m.gainB, m.lossB) ?? 0),
          m.positive / m.hours, -m.drawdown, m.n]
}

export function better(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return a[i] > b[i]
  }
  return false
}

export function getMetric(data, period, cfg, detail = false) {
  const stream = orderIds(data,streamId(data, cfg.bf, cfg.base, cfg.ef, cfg.end))
  const entries = prepare(data, stream, period)
  const plan = axisPlan(data, stream, entries, cfg.axis, cfg.floor)
  return measure(data, plan, period, cfg.block, detail)
}
