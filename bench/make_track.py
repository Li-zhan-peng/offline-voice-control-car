# -*- coding: utf-8 -*-
"""
生成"音频测试轨": 把整场评测按【真实使用流程】拼成一条长 WAV, 一次播完。

为什么用一条轨道而不是一句一播:
  单独 PlaySound 之间会隔几秒, Windows 音频设备会在这期间进入低功耗, 下一句的
  开头被吃掉一截 —— 这不是模型的问题, 却会被算成"识别失败", 把模型对比彻底污染。
  整条轨道连续播放, 设备始终醒着, 每次试验的间隔也完全一致, 于是模型之间的差异
  才真的只来自模型。

真实流程布局 (layout=flow):
  [3s 引导]
  每个试验: [唤醒词][1.2s][口令][1.3s][停下(清理)][3.0s]
  最后: [20s 纯静音]

  "停下" 是清理动作: 口令若是前进/后退/左转/右转, 电机会转起来(小车已架起),
  电机噪声会盖住下一句的唤醒词, 所以每次都用"停下"把电机停掉。它不计入成绩。

输出:
  voice_set/track_<tag>.wav   +  voice_set/track_<tag>.json (每次试验的精确时间点)
"""
import json
import os
import random
import sys
import wave
import array

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE = os.path.join(HERE, 'voice_set')

CMDS = {4: '前进', 5: '后退', 6: '左转', 7: '右转', 8: '停下', 2: '开灯', 3: '关灯'}
MOVE = {4, 5, 6, 7}
WAKE_WORD = '你好小车'
RATE = 16000
LEAD_IN = 3.0
GAP_AFTER_WAKE = 1.2
GAP_AFTER_CMD = 1.3
GAP_AFTER_CLEAN = 3.0
SILENCE_BLOCK = 5.0
N_SILENCE = 4


def read_wav(path):
    w = wave.open(path, 'rb')
    assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2, path
    data = w.readframes(w.getnframes())
    w.close()
    a = array.array('h')
    a.frombytes(data)
    return a


def speech_end_of(a, thr=400):
    for i in range(len(a) - 1, -1, -1):
        if abs(a[i]) > thr:
            return i / float(RATE)
    return len(a) / float(RATE)


def main():
    tag = sys.argv[1] if len(sys.argv) > 1 else 'run'
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    layout = sys.argv[4] if len(sys.argv) > 4 else 'block'

    random.seed(seed)
    plan = []
    for cid in CMDS:
        plan += [cid] * rounds
    random.shuffle(plan)

    wav_cache = {}

    def wav_of(name):
        if name not in wav_cache:
            wav_cache[name] = read_wav(os.path.join(VOICE, '%s.wav' % name))
        return wav_cache[name]

    out = array.array('h')
    cues = []

    def add(a, kind, cid, word, block):
        start = len(out) / float(RATE)
        out.extend(a)
        cues.append({'kind': kind, 'id': cid, 'word': word, 'block': block,
                     'start': round(start, 4),
                     'speech_end': round(start + speech_end_of(a), 4),
                     'end': round(len(out) / float(RATE), 4)})

    def silence(sec):
        out.extend(array.array('h', [0]) * int(sec * RATE))

    silence(LEAD_IN)

    if layout == 'wake':
        # 唤醒词单独测: 间隔 6 秒, 接近真实"喊一声等一下"的节奏。
        # 之前把 5 条唤醒词背靠背连放(间隔 2 秒)会明显低估唤醒率。
        for bi in range(1, 6):
            add(wav_of('wake'), 'wake', 1, WAKE_WORD, -1)
            silence(6.0)
    elif layout == 'flow':
        for bi, cid in enumerate(plan, 1):
            add(wav_of('wake'), 'wake', 1, WAKE_WORD, bi)
            silence(GAP_AFTER_WAKE)
            add(wav_of('c%d' % cid), 'cmd', cid, CMDS[cid], bi)
            silence(GAP_AFTER_CMD)
            add(wav_of('c8'), 'cleanup', 8, CMDS[8], bi)
            silence(GAP_AFTER_CLEAN)
    else:
        # block: 只念口令, 不念唤醒词 —— 唤醒门始终关着, 电机一次都不会转,
        # 识别结果不被电机噪声污染。模型对比用这一版最干净。
        for bi, cid in enumerate(plan, 1):
            add(wav_of('c%d' % cid), 'cmd', cid, CMDS[cid], bi)
            silence(GAP_AFTER_CLEAN)
        for bi in range(1, 6):
            add(wav_of('wake'), 'wake', 1, WAKE_WORD, -1)
            silence(2.0)

    silence_start = len(out) / float(RATE)
    silence(SILENCE_BLOCK * N_SILENCE)

    path = os.path.join(VOICE, 'track_%s.wav' % tag)
    w = wave.open(path, 'wb')
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(RATE)
    w.writeframes(out.tobytes())
    w.close()

    meta = {'tag': tag, 'seed': seed, 'layout': 'flow',
            'duration': len(out) / float(RATE), 'cues': cues,
            'silence_start': round(silence_start, 4),
            'silence_total': SILENCE_BLOCK * N_SILENCE}
    with open(os.path.join(VOICE, 'track_%s.json' % tag), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    print('track: %s' % path)
    print('  时长 %.0f s (%d 次试验, 每次 = 唤醒词 + 口令 + 清理)' % (meta['duration'], len(plan)))
    print('  口令顺序: %s' % ' '.join(CMDS[c] for c in plan))


if __name__ == '__main__':
    main()
