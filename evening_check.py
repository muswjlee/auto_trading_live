import os, json, sys
from datetime import datetime
from glob import glob
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.notifier import send

today = datetime.now().strftime('%Y-%m-%d')
issues = []
lines = []

# 1. 활성 전략
enabled = []
for f in glob('strategies/*.json'):
    try:
        s = json.load(open(f, encoding='utf-8'))
        if s.get('enabled'):
            enabled.append(s['id'])
    except:
        pass
lines.append(f'전략 ({len(enabled)}개): ' + ', '.join(enabled))

# 2. ERROR 로그 (오늘)
err_files = []
for f in glob('trading_*_paper.log') + ['watchdog_paper.log']:
    if not os.path.exists(f):
        continue
    count = sum(1 for l in open(f, encoding='utf-8', errors='ignore')
                if today in l and ('[ERROR]' in l or '[CRITICAL]' in l))
    if count > 0:
        err_files.append(f'{os.path.basename(f)}: {count}건')
if err_files:
    issues.append('ERROR 로그 발견')
    lines.append('⚠️ ERROR: ' + ', '.join(err_files))
else:
    lines.append('✅ 오늘 ERROR 없음')

# 3. watchdog.pid
if os.path.exists('watchdog.pid'):
    issues.append('watchdog.pid 존재')
    lines.append('⚠️ watchdog.pid 존재 (좀비 프로세스 의심)')
else:
    lines.append('✅ watchdog.pid 없음')

# 4. 미청산 포지션
try:
    pos = json.load(open('positions_paper.json', encoding='utf-8'))
    real = {k: v for k, v in pos.items() if not v.get('_reserved')}
    if real:
        issues.append(f'미청산 포지션 {len(real)}개')
        names = [v.get('name', k) for k, v in real.items()]
        lines.append(f'⚠️ 미청산 포지션 {len(real)}개: {", ".join(names)}')
    else:
        lines.append('✅ 미청산 포지션 없음')
except:
    lines.append('⚠️ positions 읽기 실패')

# 5. 토큰 만료
try:
    t = json.load(open('.token_cache_paper.json', encoding='utf-8'))
    exp = datetime.fromisoformat(t['expires'])
    hours = (exp - datetime.now()).total_seconds() / 3600
    exp_str = exp.strftime('%m-%d %H:%M')
    if hours < 0:
        # 이미 만료 — KIS API가 기동 시 자동 재발급하므로 경고 불필요
        lines.append(f'✅ 토큰 만료됨 (자동 갱신 예정): {exp_str}')
    elif hours < 1:
        issues.append(f'토큰 만료 1시간 미만 ({hours:.1f}h)')
        lines.append(f'⚠️ 토큰 만료 임박: {exp_str} ({hours:.1f}h 후)')
    else:
        lines.append(f'✅ 토큰 만료: {exp_str} ({hours:.1f}h 후)')
except:
    lines.append('⚠️ 토큰 확인 실패')

# 6. PnL 중복
try:
    pnl = json.load(open('daily_pnl_paper.json', encoding='utf-8'))
    if pnl.get('date') == today:
        seen = defaultdict(int)
        for tr in pnl.get('trades', []):
            key = f"{tr['code']}_{tr['strategy_id']}_{tr['reason']}_{tr['buy_price']}_{tr['sell_price']}"
            seen[key] += 1
        dups = [k for k, v in seen.items() if v > 1]
        if dups:
            issues.append(f'PnL 중복 {len(dups)}건')
            lines.append(f'⚠️ PnL 중복: {len(dups)}건')
        else:
            lines.append('✅ PnL 중복 없음')
    else:
        lines.append('✅ PnL 없음 (거래 없음)')
except:
    lines.append('⚠️ PnL 확인 실패')

# 전송
body = '\n'.join(lines)
if issues:
    msg = f'⚠️ <b>[저녁 점검] 확인 필요</b>\n\n{body}'
else:
    msg = f'✅ <b>[저녁 점검] 내일 가동 준비 완료</b>\n\n{body}'

send(msg)
sys.stdout.buffer.write((msg + '\n').encode('utf-8'))
