import re, sys, io
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

with open('bot_log.txt', 'r', encoding='utf-8') as f:
    lines = f.readlines()

tp_wins = []
sl_losses = []

for line in lines:
    m_tp = re.search(r'TP.*PnL = \$\+?([-\d.]+)', line)
    m_sl = re.search(r'SL.*PnL = \$\+?([-\d.]+)', line)
    if m_tp:
        tp_wins.append(float(m_tp.group(1)))
    elif m_sl:
        sl_losses.append(float(m_sl.group(1)))

total_trades = len(tp_wins) + len(sl_losses)
total_tp = sum(tp_wins)
total_sl = sum(sl_losses)
net = total_tp + total_sl
wr = len(tp_wins)/total_trades*100 if total_trades else 0
avg_win = total_tp/len(tp_wins) if tp_wins else 0
avg_loss = total_sl/len(sl_losses) if sl_losses else 0
rr = abs(avg_win/avg_loss) if avg_loss else 0

print('=== FULL LOG ANALYSIS ===')
print(f'Total trades: {total_trades}')
print(f'TP wins: {len(tp_wins)}')
print(f'SL losses: {len(sl_losses)}')
print(f'Win Rate: {wr:.1f}%')
print(f'Total TP: +${total_tp:.2f}')
print(f'Total SL: ${total_sl:.2f}')
print(f'Net PnL: ${net:.2f}')
print(f'Avg win: +${avg_win:.2f}')
print(f'Avg loss: ${avg_loss:.2f}')
print(f'Win/Loss ratio: {rr:.2f}')
print()

pair_stats = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0.0, 'count': 0})
for line in lines:
    m_tp2 = re.search(r'(\w+USDT).*TP.*PnL = \$\+?([-\d.]+)', line)
    m_sl2 = re.search(r'(\w+USDT).*SL.*PnL = \$\+?([-\d.]+)', line)
    if m_tp2:
        sym, pnl = m_tp2.group(1), float(m_tp2.group(2))
        pair_stats[sym]['wins'] += 1
        pair_stats[sym]['pnl'] += pnl
        pair_stats[sym]['count'] += 1
    elif m_sl2:
        sym, pnl = m_sl2.group(1), float(m_sl2.group(2))
        pair_stats[sym]['losses'] += 1
        pair_stats[sym]['pnl'] += pnl
        pair_stats[sym]['count'] += 1

print('=== TOP PROFITABLE PAIRS ===')
sorted_pairs = sorted(pair_stats.items(), key=lambda x: x[1]['pnl'], reverse=True)
for sym, s in sorted_pairs[:10]:
    wr2 = s['wins']/s['count']*100 if s['count'] else 0
    print(f'{sym:18s} PnL=${s["pnl"]:+8.2f} | {s["count"]:3d} trades | W:{s["wins"]} L:{s["losses"]} | WR:{wr2:.0f}%')

print()
print('=== WORST PAIRS ===')
for sym, s in sorted_pairs[-10:]:
    wr2 = s['wins']/s['count']*100 if s['count'] else 0
    print(f'{sym:18s} PnL=${s["pnl"]:+8.2f} | {s["count"]:3d} trades | W:{s["wins"]} L:{s["losses"]} | WR:{wr2:.0f}%')

# Top-5 biggest wins/losses
print()
print('=== TOP 5 BIGGEST WINS ===')
for p in sorted(tp_wins, reverse=True)[:5]:
    print(f'  +${p:.2f}')
print()
print('=== TOP 5 BIGGEST LOSSES ===')
for p in sorted(sl_losses)[:5]:
    print(f'  ${p:.2f}')
