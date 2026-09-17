# -*- coding: utf-8 -*-
"""
结论校验器 —— 从 data/ 里的原始数据重新计算 README 中声称的关键指标。

为什么要有它: "可复现"不该只是口号。文档里的每个数字都应该能从原始数据重算出来,
而且**每次提交都自动重算一遍**, 算不出来就让 CI 失败。
这样别人不必信任我们的文字, 只需要看 CI 是否通过。

用法: python validate_claims.py            # 全部通过退出码 0, 否则非 0
      python validate_claims.py --verbose  # 打印每个指标的实测值
"""
import argparse
import csv
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), 'data')
LOGS = os.path.join(DATA, 'logs')

# 与固件一致的判据(见 firmware/main/audio/wake_words/custom_wake_word.cc)
ENERGY_GATE = 1.2

results = []


def report(name, ok, detail):
    results.append((name, ok, detail))
    print('  [%s] %-46s %s' % ('PASS' if ok else 'FAIL', name, detail))


def load_cmd_rows(path):
    rows = []
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r.get('kind') == 'cmd':
                rows.append(r)
    return rows


def recall(path):
    rows = load_cmd_rows(path)
    hit = sum(1 for r in rows if r['det_id'] and r['det_id'] == r['expect_id'])
    return hit, len(rows)


def median(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else None


# ───────────── 从串口日志里提取"真口令"与"噪声触发"两类样本 ─────────────
def parse_log(log_tag, track_tag='std8'):
    """log_tag: 那次实验的日志名; track_tag: 当时用的测试轨(决定每次口令的时间点)"""
    with open(os.path.join(DATA, 'track_%s.json' % track_tag), encoding='utf-8') as f:
        meta = json.load(f)
    cues = [c for c in meta['cues'] if c['kind'] == 'cmd']

    ev = []
    with open(os.path.join(LOGS, 'bench_%s.log' % log_tag), encoding='utf-8') as f:
        for line in f:
            m = re.match(r'^\[\s*([-\d.]+)\]', line)
            if not m:
                continue
            t = float(m.group(1))
            if 'LOCAL CMD: id=' in line:
                ev.append((t, 'det', int(re.search(r'id=(\d+)', line).group(1))))
            elif 'MNPOST num=' in line:
                mm = re.search(r'MNPOST num=(\d+)((?:\s+\d+:[0-9.]+)*)', line)
                cands = [(int(i), float(p)) for i, p in
                         (tok.split(':') for tok in mm.group(2).split())]
                ev.append((t, 'post', cands))
            elif 'VOICEENERGY' in line:
                ev.append((t, 'energy', float(re.search(r'ratio=([0-9.]+)', line).group(1))))

    def attach(k):
        p = e = None
        for _t, kind, val in ev[k:k + 4]:
            if kind == 'post' and p is None:
                p = val[0][1] if val else None
            elif kind == 'energy' and e is None:
                e = val
        return p, e

    real, used = [], set()
    for c in cues:
        lo, hi = c['speech_end'] - 0.4, c['speech_end'] + 2.6
        for k, (t, kind, payload) in enumerate(ev):
            if kind == 'det' and lo <= t <= hi and payload != 1:
                used.add(k)
                real.append(attach(k))
                break
    noise = []
    for k, (t, kind, payload) in enumerate(ev):
        if kind != 'det' or k in used or payload == 1:
            continue
        # 同一句话的重复上报不算噪声(1.5 秒内且紧跟已用检测)
        if any(0 <= t - ev[u][0] <= 1.5 for u in used):
            continue
        noise.append(attach(k))
    return real, noise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    print('=' * 78)
    print('结论校验: 从原始数据重算 README 中声称的指标')
    print('=' * 78)

    # ── 1. 命令词召回率: 基线(门限 0.15) vs 部署后(门限 0.05 + 能量门) ──
    b_hit, b_n = recall(os.path.join(DATA, 'bench_mn7_cn.csv'))
    d_hit, d_n = recall(os.path.join(DATA, 'bench_mn7_deployed.csv'))
    report('基线召回率(门限 0.15) ≈ 92.9%',
           b_n == 56 and b_hit == 52,
           '%d/%d = %.1f%%' % (b_hit, b_n, 100.0 * b_hit / b_n))
    report('部署后召回率(门限 0.05+能量门) = 100%',
           d_n == 56 and d_hit == 56,
           '%d/%d = %.1f%%' % (d_hit, d_n, 100.0 * d_hit / d_n))
    report('召回层带来 +7.1 个百分点',
           abs((100.0 * d_hit / d_n) - (100.0 * b_hit / b_n) - 7.1) < 0.1,
           '%.1f -> %.1f pt' % (100.0 * b_hit / b_n, 100.0 * d_hit / d_n))

    # ── 2. 响应延迟 ──
    lat = [int(r['latency_ms']) for r in load_cmd_rows(os.path.join(DATA, 'bench_mn7_deployed.csv'))
           if r['latency_ms']]
    med = median(lat)
    report('部署后延迟中位数 ≈ 450ms (README 写"约 0.45 秒")',
           400 <= med <= 520, 'median=%dms, mean=%dms, n=%d' % (med, sum(lat) / len(lat), len(lat)))

    # ── 3. 后验概率分不开(这是驱动整个方案的负面结论) ──
    real, noise = parse_log('mn7_cn_lowthr', 'std8')
    rp = [p for p, _ in real if p is not None]
    np_ = [p for p, _ in noise if p is not None]
    report('后验概率: 真口令最低值 低于 噪声最高值(完全重叠)',
           bool(rp and np_) and min(rp) < max(np_),
           'real min=%.3f < noise max=%.3f (n_real=%d, n_noise=%d)'
           % (min(rp), max(np_), len(rp), len(np_)))

    # ── 4. 能量证据分得开(这是最终采用的方案) ──
    re_ = [e for _, e in real if e is not None]
    ne = [e for _, e in noise if e is not None]
    keep = sum(1 for v in re_ if v >= ENERGY_GATE)
    block = sum(1 for v in ne if v < ENERGY_GATE)
    report('能量证据: 门限 K=%.1f 时真口令 100%% 保留' % ENERGY_GATE,
           keep == len(re_), '保留 %d/%d' % (keep, len(re_)))
    report('能量证据: 门限 K=%.1f 挡掉 >=80%% 的噪声触发' % ENERGY_GATE,
           block >= 0.8 * len(ne), '挡掉 %d/%d = %.0f%%'
           % (block, len(ne), 100.0 * block / max(len(ne), 1)))
    # 关键对比 —— 必须在同一个约束下比才是有意义的比较:
    # 约束"必须保留 100% 的真口令", 看两种证据分别能挡掉多少噪声。
    #   · 后验概率: 要保留全部真口令, 门限就不能高于 min(real)=0.051, 此时几乎所有噪声都挡不住
    #   · 能量证据: K=1.2 时保留全部真口令, 同时挡掉大部分噪声
    # 这才是"后验概率用不了、能量证据能用"的严格表述(不能拿两个不同量纲的重叠量直接比大小)。
    tau_post = min(rp)
    block_post = sum(1 for v in np_ if v < tau_post)
    post_rate = 100.0 * block_post / max(len(np_), 1)
    energy_rate = 100.0 * block / max(len(ne), 1)
    report('同等约束下(保留100%真口令)能量证据的噪声抑制远优于后验概率',
           keep == len(re_) and energy_rate >= post_rate + 50.0,
           '能量 %.0f%% vs 后验 %.0f%% (n_noise=%d)' % (energy_rate, post_rate, len(np_)))

    # ── 5. 部署后的独立验证: 当时只有 1 次噪声触发且能量很低 ──
    real2, noise2 = parse_log('mn7_deployed', 'std8')
    re2 = [e for _, e in real2 if e is not None]
    ne2 = [e for _, e in noise2 if e is not None]
    if re2:
        report('部署后独立验证: 真口令能量比最低值 >= 门限 K=%.1f' % ENERGY_GATE,
               min(re2) >= ENERGY_GATE,
               'real min=%.2f (n=%d)' % (min(re2), len(re2)))
    if ne2:
        report('部署后独立验证: 噪声触发能量比 全部低于门限',
               all(v < ENERGY_GATE for v in ne2),
               'noise=%s' % ['%.2f' % v for v in ne2])

    # ── 汇总 ──
    fails = [r for r in results if not r[1]]
    print('=' * 78)
    print('共 %d 项检查, 通过 %d 项, 失败 %d 项' % (len(results), len(results) - len(fails), len(fails)))
    if fails:
        print('失败项:')
        for name, _ok, detail in fails:
            print('  - %s (%s)' % (name, detail))
        return 1
    print('全部结论均可由 data/ 中的原始数据复现 [OK]')
    return 0


if __name__ == '__main__':
    sys.exit(main())
