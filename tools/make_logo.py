# -*- coding: utf-8 -*-
"""
生成「言必达」项目 Logo（矢量 SVG + 位图 PNG）。

设计含义:
  三条白色音柱  = 说出来的语音指令(免手交互)
  橙色向右箭头  = 指令一定被执行 / 货物一定送达("言必达")
  深蓝圆角方块  = 可靠的设备本体
输出:
  logo_icon.png/svg       方形图标(头像/角标/PPT 小图)
  logo_horizontal.png/svg 横版组合(稿件封面/信纸抬头)
  logo_stacked.png/svg    竖版组合(工牌/展板)
"""
import os
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch, Polygon

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'logo')
os.makedirs(OUT, exist_ok=True)

for name in ('Microsoft YaHei', 'SimHei'):
    if any(name in f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams['font.sans-serif'] = [name]
        break
plt.rcParams['axes.unicode_minus'] = False

BLUE = '#123C69'      # 主色: 深蓝(可靠/医疗/专业)
ORANGE = '#FF8A3C'    # 辅色: 橙(送达/行动力)
WHITE = '#FFFFFF'
GRAY = '#5A6B7B'


def draw_mark(ax, x, y, s):
    """在 (x, y) 处画一个边长 s 的方形图标(坐标系用 0~1 归一化)"""
    # 圆角方块底
    ax.add_patch(FancyBboxPatch((x, y), s, s,
                                boxstyle="round,pad=0,rounding_size=%.4f" % (s * 0.22),
                                linewidth=0, facecolor=BLUE, zorder=1))
    # 三条音柱(语音指令)
    bw = s * 0.085
    base = y + s * 0.5
    for i, h in enumerate((0.19, 0.40, 0.26)):
        bx = x + s * (0.155 + i * 0.145)
        ax.add_patch(FancyBboxPatch((bx, base - s * h / 2), bw, s * h,
                                    boxstyle="round,pad=0,rounding_size=%.4f" % (bw / 2),
                                    linewidth=0, facecolor=WHITE, zorder=2))
    # 向右的箭头(必达)
    ay = base
    aw = s * 0.075
    ax.add_patch(FancyBboxPatch((x + s * 0.585, ay - aw / 2), s * 0.19, aw,
                                boxstyle="round,pad=0,rounding_size=%.4f" % (aw / 2),
                                linewidth=0, facecolor=ORANGE, zorder=2))
    ax.add_patch(Polygon([[x + s * 0.74, ay - s * 0.115],
                          [x + s * 0.74, ay + s * 0.115],
                          [x + s * 0.885, ay]], closed=True,
                         facecolor=ORANGE, linewidth=0, zorder=2))


def save(fig, name):
    for ext in ('png', 'svg'):
        fig.savefig(os.path.join(OUT, '%s.%s' % (name, ext)),
                    dpi=300, transparent=False, facecolor='white')
    print('  -> %s.png / %s.svg' % (name, name))


def icon():
    fig = plt.figure(figsize=(5.12, 5.12))
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis('off')
    draw_mark(ax, 0.0, 0.0, 1.0)
    save(fig, 'logo_icon'); plt.close(fig)


def horizontal():
    fig = plt.figure(figsize=(12.8, 3.6))
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 12.8); ax.set_ylim(0, 3.6); ax.axis('off')
    draw_mark(ax, 0.55, 0.55, 2.5)
    ax.text(3.6, 2.02, '言必达', fontsize=52, color=BLUE, va='center', ha='left', weight='bold')
    ax.text(3.68, 1.16, 'VoiceSure', fontsize=23, color=GRAY, va='center', ha='left',
            family='DejaVu Sans', style='italic')
    ax.plot([3.7, 12.3], [0.86, 0.86], color=ORANGE, lw=2.2)
    ax.text(3.68, 0.52, '断网也能听懂的免手语音配送机器人', fontsize=17, color=GRAY,
            va='center', ha='left')
    save(fig, 'logo_horizontal'); plt.close(fig)


def stacked():
    fig = plt.figure(figsize=(6.4, 5.2))
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 6.4); ax.set_ylim(0, 5.2); ax.axis('off')
    draw_mark(ax, 1.95, 2.05, 2.5)
    ax.text(3.2, 1.42, '言必达', fontsize=40, color=BLUE, va='center', ha='center', weight='bold')
    ax.text(3.2, 0.86, 'VoiceSure', fontsize=17, color=GRAY, va='center', ha='center',
            family='DejaVu Sans', style='italic')
    ax.text(3.2, 0.36, '断网也能听懂的免手语音配送机器人', fontsize=12, color=GRAY,
            va='center', ha='center')
    save(fig, 'logo_stacked'); plt.close(fig)


if __name__ == '__main__':
    print('生成 Logo ->', OUT)
    icon(); horizontal(); stacked()
    print('完成')
