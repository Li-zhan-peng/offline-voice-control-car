# -*- coding: utf-8 -*-
"""
判决规则优化器 —— 从实测后验数据里学出比"取 top-1"更好的规则。

背景:
  ESP-SR 的 MultiNet 在 prob[0] 超过一个全局门限时就吐出结果, 固件直接执行 top-1。
  但模型其实给了 top-5 候选和后验概率。把门限压低当"召回层", 再用我们自己的
  规则做"精度层", 就有改进空间 —— 这就是本项目算法创新的落点。

候选规则:
  score[i] = prob[i] + bias[command_id[i]]        # 偏置校正(某些口令天生后验偏高)
  接受条件: score_max >= tau          (绝对置信度)
            且 score_max - score_2nd >= margin   (边际: 分不清就宁可不动)
  不满足就拒识 —— 对小车来说"没听懂"远比"听错方向"安全。

评估方式:
  实测数据按轮次做 2 折交叉验证 —— 一半试验上搜参数, 另一半上评估,
  只报留出集(held-out)的成绩, 避免自己给自己打分虚高。

用法: python _analyze_rules.py --tag mn7_cn --track std8
"""
import argparse
import csv
import json
import os
import re
import sys
import itertools

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE = os.path.join(HERE, 'voice_set')
CMDS = {4: '前进', 5: '后退', 6: '左转', 7: '右转', 8: '停下', 2: '开灯', 3: '关灯'}
WAKE_ID = 1
RE_LINE = re.compile(r'^\[\s*([-\d.]+)\]\s+.*?(?:LOCAL CMD: id=\d+|MNPOST num=|ACTION:)')
RE_TS = re.compile(r'^\[\s*([-\d.]+)\]')
RE_MNPOST = re.compile(r'MNPOST num=(\d+)((?:\s+\d+:[0-9.]+)*)')
RE_LOCAL = re.compile(r'LOCAL CMD: id=(\d+)')
RE_ACTION = re.compile(r'ACTION: (\w+)')
RE_ENERGY = re.compile(r'VOICEENERGY ratio=([0-9.]+) peak=([0-9.]+) noise=([0-9.]+)')


def load_samples(tag, track):
    with open(os.path.join(VOICE, 'track_%s.json' % track), encoding='utf-8') as f:
        meta = json.load(f)
    cues = [c for c in meta['cues'] if c['kind'] == 'cmd']

    events = []          # (t, kind, payload)
    with open(os.path.join(HERE, 'bench_%s_serial.log' % tag), encoding='utf-8') as f:
        for line in f:
            m = RE_TS.match(line)
            if not m:
                continue
            t = float(m.group(1))
            mm = RE_LOCAL.search(line)
            if mm:
                events.append((t, 'det', int(mm.group(1))))
                continue
            mm = RE_MNPOST.search(line)
            if mm:
                cands = [(int(i), float(p)) for i, p in
                         (tok.split(':') for tok in mm.group(2).split())]
                events.append((t, 'post', cands))
                continue
            mm = RE_ENERGY.search(line)
            if mm:
                events.append((t, 'energy', float(mm.group(1))))

    samples = []
    used = set()
    for ci, c in enumerate(cues):
        lo, hi = c['speech_end'] - 0.4, c['speech_end'] + 2.6
        det = None
        for k, (t, kind, payload) in enumerate(events):
            if kind != 'det' or not (lo <= t <= hi) or payload == WAKE_ID:
                continue
            det = k
            used.add(k)
            break
        cands = []
        det_id = None
        energy = None
        if det is not None:
            det_id = events[det][2]
            for t, kind, payload in events[det:det + 4]:
                if kind == 'post' and not cands:
                    cands = payload
                elif kind == 'energy' and energy is None:
                    energy = payload
        samples.append({'block': ci // len(CMDS) + 1,
                        'expect': c['id'], 'expect_word': c['word'],
                        'det_id': det_id,
                        'top1': cands[0][0] if cands else det_id,
                        'cands': cands, 'energy': energy})

    # 一次说话里重复报的检测(低门限时很常见)与真正凭空误触发要区分开
    dup = 0
    strays = []
    for k, (t, kind, payload) in enumerate(events):
        if kind != 'det' or k in used or payload == WAKE_ID:
            continue
        near = False
        for s_k in used:
            if 0 <= t - events[s_k][0] <= 1.5:
                near = True
                break
        if near:
            dup += 1
            continue
        cands = []
        energy = None
        for t2, kind2, payload2 in events[k:k + 4]:
            if kind2 == 'post' and not cands:
                cands = payload2
            elif kind2 == 'energy' and energy is None:
                energy = payload2
        strays.append({'t': t, 'id': payload, 'cands': cands,
                       'top1': cands[0][1] if cands else None,
                       'margin': (cands[0][1] - cands[1][1]) if len(cands) > 1 else None,
                       'energy': energy})
    return samples, meta, dup, strays


def decide(cands, bias, tau, margin):
    if not cands:
        return None
    sc = sorted(((p + bias.get(i, 0.0), i) for i, p in cands), reverse=True)
    top = sc[0]
    second = sc[1][0] if len(sc) > 1 else 0.0
    if top[0] < tau:
        return None
    if top[0] - second < margin:
        return None
    return top[1]


def score(samples, bias, tau, margin, off_by=0):
    """返回 (正确数, 错误执行数, 拒识数, 总数)"""
    ok = wrong = rej = 0
    for s in samples:
        if s['block'] % 2 != off_by:
            continue
        d = decide(s['cands'], bias, tau, margin)
        if d is None:
            rej += 1
        elif d == s['expect']:
            ok += 1
        else:
            wrong += 1
    return ok, wrong, rej, ok + wrong + rej


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', required=True)
    ap.add_argument('--track', default='std8')
    ap.add_argument('--write', action='store_true')
    args = ap.parse_args()

    samples, meta, dup, strays = load_samples(args.tag, args.track)
    n = len(samples)
    with_c = [s for s in samples if s['cands']]
    L = []
    A = L.append
    A('=' * 70)
    A('判决规则优化报告   模型: %s   样本: %d 次' % (args.tag, n))
    A('=' * 70)
    A('有后验候选数据的样本: %d / %d (%.1f%%)' % (len(with_c), n, 100.0 * len(with_c) / max(n, 1)))
    A('同一句重复上报的检测: %d 次(低门限时常见, 不算误触发)' % dup)
    base_ok = sum(1 for s in samples if s['top1'] == s['expect'])
    base_rej = sum(1 for s in samples if s['top1'] is None)
    base_wrong = n - base_ok - base_rej
    A('')
    A('[基线] 直接用模型 top-1 (当前固件行为)')
    A('    正确 %d / %d = %.1f%%   错误执行 %d   没反应 %d'
      % (base_ok, n, 100.0 * base_ok / n, base_wrong, base_rej))

    # ---- 1. 后验分布: 看看"对"和"错"的置信度是否可分 ----
    good = [s['cands'][0][1] for s in with_c if s['top1'] == s['expect']]
    bad = [s['cands'][0][1] for s in with_c if s['top1'] != s['expect']]
    A('')
    A('[1] top-1 后验置信度分布')
    if good:
        A('    判对时: 平均 %.3f, 最低 %.3f, 最高 %.3f' % (sum(good) / len(good), min(good), max(good)))
    if bad:
        A('    判错时: 平均 %.3f, 最低 %.3f, 最高 %.3f' % (sum(bad) / len(bad), min(bad), max(bad)))
    # ---- 2. 边际(第一名减第二名) ----
    gm = [s['cands'][0][1] - (s['cands'][1][1] if len(s['cands']) > 1 else 0) for s in with_c if s['top1'] == s['expect']]
    bm = [s['cands'][0][1] - (s['cands'][1][1] if len(s['cands']) > 1 else 0) for s in with_c if s['top1'] != s['expect']]
    A('')
    A('[2] 边际 (top1 后验 - top2 后验)')
    if gm:
        A('    判对时: 平均 %.3f' % (sum(gm) / len(gm)))
    if bm:
        A('    判错时: 平均 %.3f' % (sum(bm) / len(bm)))

    # ---- 2.6 能量证据: 真口令 vs 噪声误触发 ----
    en_real = [s['energy'] for s in samples if s['energy'] is not None]
    en_fake = [s['energy'] for s in strays if s['energy'] is not None]
    A('')
    A('[2.5] 能量证据 (检测时刻 最近1秒峰值RMS / 环境噪声底)')
    if en_real:
        A('    真口令  : %d 个, 平均 %.2f, 最低 %.2f, 最高 %.2f'
          % (len(en_real), sum(en_real) / len(en_real), min(en_real), max(en_real)))
    if en_fake:
        A('    噪声触发: %d 个, 平均 %.2f, 最低 %.2f, 最高 %.2f'
          % (len(en_fake), sum(en_fake) / len(en_fake), min(en_fake), max(en_fake)))
    if en_real and en_fake:
        A('    -> 真口令最低 %.2f vs 噪声最高 %.2f : %s'
          % (min(en_real), max(en_fake),
             '完全不重叠, 用一个能量门限就能分开' if min(en_real) > max(en_fake)
             else '有重叠, 需要配合其他条件'))
        A('')
        A('    能量门限扫描 (K): 保留多少真口令 / 挡掉多少噪声触发')
        A('      K     真口令保留        噪声挡掉')
        for K in [1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 4.0, 5.0]:
            keep = sum(1 for v in en_real if v >= K)
            block = sum(1 for v in en_fake if v < K)
            A('      %-5.1f %3d/%-3d (%5.1f%%)   %3d/%-3d (%5.1f%%)'
              % (K, keep, len(en_real), 100.0 * keep / len(en_real),
                 block, len(en_fake), 100.0 * block / len(en_fake)))

    A('')
    A('[2.6] 没有口令时的凭空触发 —— %d 次' % len(strays))
    if strays:
        A('    时刻s   报成       后验  边际  能量比')
        for s in strays:
            A('    %6.1f  %-8s  %s  %s  %s' % (
                s['t'], CMDS.get(s['id'], s['id']),
                ('%.3f' % s['top1']) if s['top1'] is not None else '-',
                ('%.3f' % s['margin']) if s['margin'] is not None else '-',
                ('%.2f' % s['energy']) if s.get('energy') is not None else '-'))
        P = [s['top1'] for s in strays if s['top1'] is not None]
        M = [s['margin'] for s in strays if s['margin'] is not None]
        if P:
            A('    凭空触发的后验: 平均 %.3f, 最大 %.3f' % (sum(P) / len(P), max(P)))
        if M:
            A('    凭空触发的边际: 平均 %.3f, 最大 %.3f' % (sum(M) / len(M), max(M)))
        A('    -> 口令时的后验最低 %.3f, 凭空触发最高 %.3f: %s'
          % (min(good) if good else -1,
             max(P) if P else -1,
             '两者有重叠, 单靠后验分不开' if (P and good and max(P) >= min(good)) else '两者可分'))

    # ---- 3. 混淆对 ----
    A('')
    A('[3] 模型把谁听成了谁 (top-1)')
    conf = {}
    for s in samples:
        if s['top1'] is not None and s['top1'] != s['expect']:
            conf[(s['expect'], s['top1'])] = conf.get((s['expect'], s['top1']), 0) + 1
    if conf:
        for (e, d), c in sorted(conf.items(), key=lambda kv: -kv[1]):
            A('    %s -> %s : %d 次' % (CMDS.get(e, e), CMDS.get(d, d), c))
    else:
        A('    无误判')

    # ---- 4. 纯拒识规则: 网格扫 (tau, margin) ----
    A('')
    A('[4] 拒识规则网格 (不含偏置) —— 用小车的代价函数排序')
    A('    代价 = 3 x 错误执行 + 1 x 没反应   (听错方向比没反应危险得多)')
    best = None
    rows = []
    for tau in [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]:
        for margin in [0.0, 0.05, 0.10, 0.15, 0.20, 0.30]:
            ok, wrong, rej, tot = score(samples, {}, tau, margin)
            cost = 3 * wrong + rej
            rows.append((cost, tau, margin, ok, wrong, rej, tot))
            if best is None or cost < best[0]:
                best = (cost, tau, margin, ok, wrong, rej, tot)
    rows.sort()
    A('    tau  margin |  正确  错误执行  没反应   代价')
    for cost, tau, margin, ok, wrong, rej, tot in rows[:8]:
        A('    %.2f %.2f   |  %3d    %3d      %3d    %3d' % (tau, margin, ok, wrong, rej, cost))
    A('    基线代价 = 3 x %d + %d = %d' % (base_wrong, base_rej, 3 * base_wrong + base_rej))

    # ---- 5. 学一个偏置向量 (2 折交叉验证) ----
    A('')
    A('[5] 学一个每命令偏置 bias[id] (2 折交叉验证, 只报留出集)')
    biases = [0.0, -0.05, -0.10, -0.15, 0.05, 0.10]
    taus = [0.0, 0.15, 0.20, 0.25, 0.30, 0.40]
    margins = [0.0, 0.05, 0.10, 0.15, 0.20]

    def train_on(train, cmds):
        # 逐坐标上升: 每个命令找一个偏置, 让训练集代价最低
        bias = {c: 0.0 for c in cmds}
        for _ in range(2):
            for c in cmds:
                bestv, bestc = 0.0, None
                for v in biases:
                    bias[c] = v
                    ok, wrong, rej, tot = score(train, bias, 0.0, 0.0)
                    cost = 3 * wrong + rej
                    if bestc is None or cost < bestc:
                        bestc, bestv = cost, v
                bias[c] = bestv
        # 再在偏置固定后选 tau/margin
        bt, bm_, bc = 0.0, 0.0, None
        for tau in taus:
            for m in margins:
                ok, wrong, rej, tot = score(train, bias, tau, m)
                cost = 3 * wrong + rej
                if bc is None or cost < bc:
                    bc, bt, bm_ = cost, tau, m
        return bias, bt, bm_

    tot_ok = tot_wrong = tot_rej = tot_n = 0
    for fold in (0, 1):
        train = [s for s in samples if s['block'] % 2 == fold]
        test = [s for s in samples if s['block'] % 2 != fold]
        bias, tau, m = train_on(train, list(CMDS))
        ok, wrong, rej, tot = score(test, bias, tau, m, off_by=1 - fold)
        tot_ok += ok
        tot_wrong += wrong
        tot_rej += rej
        tot_n += tot
        A('    折%d: bias=%s tau=%.2f margin=%.2f -> 留出集 正确 %d/%d, 错误 %d, 没反应 %d'
          % (fold + 1, {CMDS.get(k, k): round(v, 2) for k, v in bias.items()}, tau, m, ok, tot, wrong, rej))
    A('    留出集合计: 正确 %d / %d = %.1f%%, 错误执行 %d, 没反应 %d, 代价 %d'
      % (tot_ok, tot_n, 100.0 * tot_ok / max(tot_n, 1), tot_wrong, tot_rej, 3 * tot_wrong + tot_rej))
    A('    基线(同口径): 正确 %d / %d = %.1f%%, 错误执行 %d, 没反应 %d, 代价 %d'
      % (base_ok, n, 100.0 * base_ok / n, base_wrong, base_rej, 3 * base_wrong + base_rej))
    A('=' * 70)

    txt = '\n'.join(L)
    print(txt)
    if args.write:
        p = os.path.join(HERE, '规则优化_%s.txt' % args.tag)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(txt + '\n')
        print('\nwrote %s' % p)
    # 明细导出, 便于人工检查
    with open(os.path.join(HERE, 'samples_%s.csv' % args.tag), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['block', 'expect_id', 'expect_word', 'top1_id', 'top1_word',
                    'top1_prob', 'top2_id', 'top2_prob', 'margin', 'cands'])
        for s in samples:
            c = s['cands']
            w.writerow([s['block'], s['expect'], s['expect_word'],
                        s['top1'], CMDS.get(s['top1'], '') if s['top1'] else '',
                        ('%.3f' % c[0][1]) if c else '',
                        c[1][0] if len(c) > 1 else '',
                        ('%.3f' % c[1][1]) if len(c) > 1 else '',
                        ('%.3f' % (c[0][1] - c[1][1])) if len(c) > 1 else '',
                        ' '.join('%d:%.3f' % (i, p) for i, p in c)])
    return 0


if __name__ == '__main__':
    sys.exit(main())
