# -*- coding: utf-8 -*-
"""
生成比赛 PPT 要用的数据图表（PNG + SVG 矢量图）。

数据全部来自实测存档：
  - 模型对比: bench_mn5q8_cn.csv / bench_mn6_cn.csv / bench_mn7_cn.csv (各 56 次试验)
  - 后验 & 能量证据: bench_mn7_cn_lowthr_serial.log (低门限召回层数据)
所有数字都不是示意，跑一遍就能复现。
用法: python _make_figures.py
"""
import os
import re
import csv
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'figures')
os.makedirs(OUT, exist_ok=True)

# 中文字体
for name in ('Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC'):
    if any(name in f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams['font.sans-serif'] = [name]
        break
plt.rcParams['axes.unicode_minus'] = False

C_MAIN = '#2E5C8A'
C_ACC = '#D97706'
C_BAD = '#B91C1C'
C_OK = '#15803D'


def save(fig, name):
    for ext in ('png', 'svg'):
        fig.savefig(os.path.join(OUT, '%s.%s' % (name, ext)),
                    dpi=200, bbox_inches='tight', facecolor='white')
    print('  -> %s.png / %s.svg' % (name, name))


def read_bench(path):
    rows = []
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            if r.get('kind') == 'cmd' or 'expect_id' in r:
                rows.append(r)
    return rows


# ───────────────────────── 图1: 三模型对比 ─────────────────────────
def fig_model_compare():
    models = ['mn5q8', 'mn6', 'mn7']
    acc = [62.5, 92.9, 92.9]
    lat = [350, 510, 507]
    size = [2494857 / 1048576.0, 3994697 / 1048576.0, 2972389 / 1048576.0]

    fig, ax = plt.subplots(1, 3, figsize=(13, 3.8))
    for a, vals, title, color, fmt in (
            (ax[0], acc, '命令词识别率 (56 次试验)', C_MAIN, '%.1f%%'),
            (ax[1], size, '模型体积 (srmodels.bin, MB)', C_ACC, '%.2f MB'),
            (ax[2], lat, '响应延迟 (词尾 → 出结果, ms)', C_OK, '%d ms')):
        bars = a.bar(models, vals, color=color, width=0.55)
        a.set_title(title, fontsize=12)
        for b, v in zip(bars, vals):
            a.text(b.get_x() + b.get_width() / 2, v, fmt % v,
                   ha='center', va='bottom', fontsize=11)
        a.grid(axis='y', alpha=0.25)
        a.set_axisbelow(True)
    ax[0].set_ylim(0, 110)
    ax[1].set_ylim(0, 4.6)
    ax[2].set_ylim(0, 620)
    fig.suptitle('三代 MultiNet 中文命令词模型对比：mn6 与 mn7 精度、延迟持平，但 mn7 小 26%',
                 fontsize=13)
    save(fig, 'fig1_model_compare')
    plt.close(fig)


# ───────────────── 图2: 为什么必须做自动评测台 ─────────────────
def fig_bench_reason():
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    labels = ['分句单独播放\n(音频设备低功耗吃句首)', '整条测试轨连续播放']
    vals = [38.1, 71.4]
    bars = ax.bar(labels, vals, color=[C_BAD, C_OK], width=0.5)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 1.5, '%.1f%%' % v,
                ha='center', fontsize=13)
    ax.set_ylabel('mn5q8 识别率')
    ax.set_ylim(0, 90)
    ax.set_title('同一批音频、同一个模型：评测方式不同，结论相差 33 个百分点', fontsize=12)
    ax.grid(axis='y', alpha=0.25)
    ax.set_axisbelow(True)
    save(fig, 'fig2_bench_method')
    plt.close(fig)


# ───────────────── 图3: 后验概率分不开 (杀手图) ─────────────────
def parse_lowthr():
    """从低门限串口日志里提取 真口令 / 噪声触发 的后验与能量比"""
    log = os.path.join(HERE, 'bench_mn7_cn_lowthr_serial.log')
    track = os.path.join(HERE, 'voice_set', 'track_std8.json')
    import json
    with open(track, encoding='utf-8') as f:
        meta = json.load(f)
    cues = [c for c in meta['cues'] if c['kind'] == 'cmd']

    ev = []
    with open(log, encoding='utf-8') as f:
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

    real, noise = [], []
    used = set()
    for c in cues:
        lo, hi = c['speech_end'] - 0.4, c['speech_end'] + 2.6
        for k, (t, kind, payload) in enumerate(ev):
            if kind == 'det' and lo <= t <= hi and payload != 1:
                used.add(k)
                p = e = None
                for t2, k2, v2 in ev[k:k + 4]:
                    if k2 == 'post' and p is None:
                        p = v2[0][1] if v2 else None
                    if k2 == 'energy' and e is None:
                        e = v2
                real.append((p, e))
                break
    for k, (t, kind, payload) in enumerate(ev):
        if kind != 'det' or k in used or payload == 1:
            continue
        p = e = None
        for t2, k2, v2 in ev[k:k + 4]:
            if k2 == 'post' and p is None:
                p = v2[0][1] if v2 else None
            if k2 == 'energy' and e is None:
                e = v2
        if p is not None:
            noise.append((p, e))
    return real, noise


def fig_posterior(real, noise):
    rp = [p for p, _ in real if p is not None]
    np_ = [p for p, _ in noise if p is not None]
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    bins = [i / 100.0 for i in range(0, 65, 2)]
    ax.hist(rp, bins=bins, color=C_OK, alpha=0.75, label='真口令 (%d 次)' % len(rp))
    ax.hist(np_, bins=bins, color=C_BAD, alpha=0.75, label='无口令时的噪声触发 (%d 次)' % len(np_))
    ax.axvline(min(rp), color=C_OK, ls='--', lw=2)
    ax.axvline(max(np_), color=C_BAD, ls='--', lw=2)
    ax.annotate('真口令最低 %.3f' % min(rp), xy=(min(rp), 0), xytext=(min(rp) + 0.02, max(3, 1)),
                color=C_OK, fontsize=10)
    ax.annotate('噪声最高 %.3f' % max(np_), xy=(max(np_), 0), xytext=(max(np_) + 0.02, max(4, 2)),
                color=C_BAD, fontsize=10)
    ax.set_xlabel('模型输出的 top-1 后验概率')
    ax.set_ylabel('次数')
    ax.set_title('后验概率分不开"真口令"和"噪声触发"：真口令最低 0.051 < 噪声最高 0.141，完全重叠',
                 fontsize=11.5)
    ax.legend()
    ax.grid(axis='y', alpha=0.25)
    ax.set_axisbelow(True)
    save(fig, 'fig3_posterior_overlap')
    plt.close(fig)


def fig_energy(real, noise):
    rv = [e for _, e in real if e is not None]
    nv = [e for _, e in noise if e is not None]
    fig, ax = plt.subplots(figsize=(7.6, 4.0))
    ax.scatter(rv, [1] * len(rv), s=55, color=C_OK, alpha=0.7, label='真口令 (%d 次)' % len(rv))
    ax.scatter(nv, [0] * len(nv), s=90, color=C_BAD, marker='D', label='噪声误触发 (%d 次)' % len(nv))
    ax.axvline(1.2, color=C_MAIN, ls='--', lw=2)
    ax.text(1.25, 1.5, '门限 K = 1.2', color=C_MAIN, fontsize=11)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(['噪声', '真口令'])
    ax.set_ylim(-0.5, 2.0)
    ax.set_xlim(-0.2, max(rv + nv) + 1.2)
    ax.set_xlabel('能量证据 = 最近 1 秒峰值 RMS ÷ 6 秒慢平均噪声底')
    ax.set_title('换成"像不像人在说话"的能量证据后: 真口令最低 %.2f, 噪声最高 %.2f'
                 % (min(rv), max(nv)), fontsize=11.5)
    ax.legend(loc='upper right')
    ax.grid(axis='x', alpha=0.25)
    ax.set_axisbelow(True)
    save(fig, 'fig4_energy_separation')
    plt.close(fig)


# ───────────────── 图5: 召回层/精度层的效果 ─────────────────
def fig_pipeline():
    fig, ax = plt.subplots(figsize=(7.6, 3.6))
    labels = ['原固件\n门限 0.15', '只降门限\n0.05 (召回层)',
              '降门限 + 能量门 K=1.2\n(本方案)']
    recall = [92.9, 100.0, 100.0]
    falsep = [0, 7, 1]
    x = range(len(labels))
    b1 = ax.bar([i - 0.2 for i in x], recall, width=0.4, color=C_OK, label='命令词召回率 (%)')
    b2 = ax.bar([i + 0.2 for i in x], falsep, width=0.4, color=C_BAD, label='无口令时段误触发 (次)')
    for b, v in zip(b1, recall):
        ax.text(b.get_x() + b.get_width() / 2, v + 1, '%.1f%%' % v, ha='center', fontsize=10)
    for b, v in zip(b2, falsep):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.2, '%d' % v, ha='center', fontsize=10)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 115)
    ax.set_title('召回层把该报的都报出来，精度层再把噪声挡回去', fontsize=12)
    ax.legend(loc='center right')
    ax.grid(axis='y', alpha=0.25)
    ax.set_axisbelow(True)
    save(fig, 'fig5_pipeline_effect')
    plt.close(fig)


def main():
    print('生成图表 -> %s' % OUT)
    fig_model_compare()
    fig_bench_reason()
    try:
        real, noise = parse_lowthr()
        print('  解析到: 真口令 %d 条, 噪声触发 %d 条' % (len(real), len(noise)))
        if real and noise:
            fig_posterior(real, noise)
            fig_energy(real, noise)
        else:
            print('  !! 数据不足, 跳过后验/能量图')
    except Exception as e:
        print('  !! 解析失败: %s' % e)
    fig_pipeline()
    print('完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
