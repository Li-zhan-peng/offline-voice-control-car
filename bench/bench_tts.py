# -*- coding: utf-8 -*-
"""
语音指令识别 —— 自动化评测台 v2  (TTS 固定语音回放 + 串口打点)

为什么要这样做:
  人工反复念口令, 每次音量/语速/距离都不同, 三个模型之间无法公平比较。
  这里用同一批固定 WAV 反复回放, 让"发音"这个变量完全冻结, 于是识别率/延迟
  的差异就只来自模型本身。

三个子实验:
  A. 指令识别  : 逐个回放指令 WAV, 统计识别率 / 混淆矩阵 / 响应延迟
  B. 唤醒词    : 回放唤醒词 WAV, 统计唤醒成功率
  C. 静音误触发: 不放任何声音, 统计凭空冒出的检测次数

时间对齐原理:
  PC 端用单调时钟记录 WAV 的播放起止; 板子通过串口回传识别结果;
  再用 WAV 波形里"最后一个有效采样点"作为真正的词尾, 得到
  响应延迟 = 词尾 -> 板子打出识别日志 的时间差。

用法:
  python _bench_tts.py --tag mn7_cn --rounds 3      # 完整评测
  python _bench_tts.py --tag smoke --quick          # 冒烟(只用"停下", 不驱动电机)
"""
import argparse
import os
import re
import sys
import time
import wave
import random
import array
import threading
import queue
import csv
import subprocess

try:
    import winsound
except ImportError:
    winsound = None

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE = os.path.join(HERE, 'voice_set')

# 与固件 esp_mn_commands_add 的顺序一致
CMDS = {4: '前进', 5: '后退', 6: '左转', 7: '右转', 8: '停下', 2: '开灯', 3: '关灯'}
WAKE_ID = 1
MOVE_IDS = {4, 5, 6, 7}

RE_CMD = re.compile(r'=== LOCAL CMD: id=(\d+), word=([^,]+), prob=([0-9.]+) ===')
RE_ACTION = re.compile(r'ACTION: (\w+)')
RE_IGNORED = re.compile(r'cmd (\d+) ignored')


def wav_meta(path):
    """返回 (总时长秒, 词尾时刻秒) —— 词尾用最后一个超过阈值的采样点估计"""
    with wave.open(path, 'rb') as w:
        n, rate = w.getnframes(), w.getframerate()
        raw = w.readframes(n)
    dur = n / float(rate)
    a = array.array('h')
    a.frombytes(raw[:len(raw) // 2 * 2])
    thr = 400
    last = None
    for i in range(len(a) - 1, -1, -1):
        if abs(a[i]) > thr:
            last = i
            break
    speech_end = (last / float(rate)) if last is not None else dur
    return dur, speech_end


class Board:
    """后台线程读串口, 给每一行打上 PC 端单调时钟时间戳"""

    def __init__(self, port):
        self.ser = serial.Serial(port, 115200, timeout=0.05)
        self.events = []
        self._stop = False
        self._buf = b''
        self._th = threading.Thread(target=self._reader, daemon=True)
        self._th.start()

    def _reader(self):
        while not self._stop:
            try:
                d = self.ser.read(2048)
            except Exception:
                break
            if not d:
                continue
            now = time.monotonic()
            self._buf += d
            while b'\n' in self._buf:
                line, self._buf = self._buf.split(b'\n', 1)
                txt = line.decode('utf-8', 'ignore').strip()
                if txt:
                    self.events.append((now, txt))

    def reset(self):
        self.events.clear()

    def since(self, mark, pattern):
        out = []
        for t, txt in list(self.events):
            if t < mark:
                continue
            m = pattern.search(txt)
            if m:
                out.append((t, m))
        return out

    def cmds_since(self, mark):
        return [(t, int(m.group(1)), m.group(2), float(m.group(3)))
                for t, m in self.since(mark, RE_CMD)]

    def close(self):
        self._stop = True
        time.sleep(0.15)
        try:
            self.ser.close()
        except Exception:
            pass

    def wait_ready(self, timeout=75):
        """等板子打印出 'N active speech commands' —— 模型加载完成, 可以开始测了。
        同时把模型名从启动日志里抠出来, 写进报告做凭证。"""
        t_end = time.monotonic() + timeout
        model = None
        t_seen = None
        while time.monotonic() < t_end:
            for _t, txt in list(self.events):
                m = re.search(r'multinet:\s*(\S+)', txt)
                if m:
                    model = m.group(1)
                    t_seen = _t
                if 'active speech commands' in txt:
                    return model, True
            # 有的模型(MultiNet5)不打 "active speech commands", 看到模型名后再稳 4 秒就算就绪
            if t_seen and time.monotonic() - t_seen > 4.0:
                return model, True
            time.sleep(0.1)
        return model, False


def play(path):
    t0 = time.monotonic()
    if winsound:
        winsound.PlaySound(path, winsound.SND_FILENAME)
    return t0, time.monotonic()


def run_cmd_trial(board, cid, listen=3.0):
    """回放一个指令词, 等识别结果。不做任何电机动作(唤醒门自然关闭)"""
    wav = os.path.join(VOICE, 'c%d.wav' % cid)
    dur, speech_end = wav_meta(wav)
    mark = time.monotonic()
    t0, t1 = play(wav)
    r = {'expect_id': cid, 'det_id': None, 'det_word': None, 'prob': None,
         'lat_from_speech': None, 'lat_from_end': None, 'audio_ms': int(dur * 1000),
         'extra': 0, 'ignored': 0, 'action': '', 'wake_fp': 0}
    limit = t1 + listen
    while time.monotonic() < limit:
        got = [c for c in board.cmds_since(mark) if c[1] != WAKE_ID]
        if got:
            t, gid, word, prob = got[0]
            r.update(det_id=gid, det_word=word, prob=prob)
            r['lat_from_speech'] = int((t - (t0 + speech_end)) * 1000)
            r['lat_from_end'] = int((t - t1) * 1000)
            r['extra'] = len(got) - 1
            break
        time.sleep(0.02)
    r['ignored'] = len(board.since(mark, RE_IGNORED))
    r['wake_fp'] = len([c for c in board.cmds_since(mark) if c[1] == WAKE_ID])
    acts = board.since(mark, RE_ACTION)
    r['action'] = acts[0][1].group(1) if acts else ''
    return r


def run_wake_trial(board, listen=3.0):
    wav = os.path.join(VOICE, 'wake.wav')
    dur, speech_end = wav_meta(wav)
    mark = time.monotonic()
    t0, t1 = play(wav)
    r = {'ok': 0, 'prob': None, 'lat_ms': None}
    limit = t1 + listen
    while time.monotonic() < limit:
        got = [c for c in board.cmds_since(mark) if c[1] == WAKE_ID]
        if got:
            t, _id, word, prob = got[0]
            r.update(ok=1, prob=prob, lat_ms=int((t - (t0 + speech_end)) * 1000))
            break
        time.sleep(0.02)
    return r


def run_silence(board, seconds=5.0):
    mark = time.monotonic()
    time.sleep(seconds)
    return len([c for c in board.cmds_since(mark) if c[1] != WAKE_ID])


def run_track(board, tag, args, model):
    """整条测试轨一次播完, 事后把串口事件按时间轴对齐到每次试验上。

    时间基准: PlaySound 是阻塞的, 所以"播放结束时刻 - 轨道总时长" = 轨道 0 秒
    对应的本机时钟。用结束点对齐比用开始点准(开始点含音频设备打开延迟)。
    """
    import json
    meta_path = os.path.join(VOICE, 'track_%s.json' % tag)
    wav_path = os.path.join(VOICE, 'track_%s.wav' % tag)
    with open(meta_path, encoding='utf-8') as f:
        meta = json.load(f)
    dur = meta['duration']

    print('播放测试轨 %.1f 秒 ...' % dur)
    mark = time.monotonic()
    t0, t1 = play(wav_path)
    origin = t1 - dur
    print('  播放结束, 偏差估计 %.0f ms' % ((t1 - t0 - dur) * 1000))

    dets = []
    for t, txt in list(board.events):
        if t < mark:
            continue
        m = RE_CMD.search(txt)
        if m:
            dets.append({'t': t - origin, 'id': int(m.group(1)),
                         'word': m.group(2), 'prob': float(m.group(3)), 'used': False})
    acts = [(t - origin, m.group(1)) for t, m in board.since(mark, RE_ACTION)]
    igns = len(board.since(mark, RE_IGNORED))

    def take(lo, hi, want_wake):
        """取窗口内第一个检测"""
        for d in dets:
            if d['used'] or not (lo <= d['t'] <= hi):
                continue
            if want_wake and d['id'] != WAKE_ID:
                continue
            if (not want_wake) and d['id'] == WAKE_ID:
                continue
            d['used'] = True
            return d
        return None

    rows, wakes = [], []
    blocks = {}
    for c in meta['cues']:
        lo, hi = c['speech_end'] - 0.4, c['speech_end'] + 2.6
        bi = c.get('block')
        if c['kind'] == 'cmd':
            d = take(lo, hi, False)
            rows.append({'expect_id': c['id'], 'expect_word': c['word'],
                         'det_id': d['id'] if d else None,
                         'det_word': d['word'] if d else None,
                         'prob': d['prob'] if d else None,
                         'lat': int((d['t'] - c['speech_end']) * 1000) if d else None})
            blocks.setdefault(bi, {})['cmd'] = (d is not None and d['id'] == c['id'])
        elif c['kind'] == 'wake':
            d = take(lo, hi, True)
            wakes.append({'ok': 1 if d else 0, 'prob': d['prob'] if d else None,
                          'lat': int((d['t'] - c['speech_end']) * 1000) if d else None})
            if bi and bi > 0:
                blocks.setdefault(bi, {})['wake'] = d is not None
        else:
            # 清理用的"停下": 只吃掉检测事件, 不参与计分
            d = take(lo, hi, False)
            if d:
                d['cleanup'] = True

    # 没被任何窗口消化掉的检测 = 误触发; 静音段里的单独统计
    stray = [d for d in dets if not d['used']]
    fp_quiet = len([d for d in stray if d['t'] >= meta['silence_start']])
    wake_fp = len([d for d in stray if d['id'] == WAKE_ID])
    joint = sum(1 for b in blocks.values() if b.get('wake') and b.get('cmd'))

    # ---------------- 统计 ----------------
    n = len(rows)
    hit = sum(1 for r in rows if r['det_id'] == r['expect_id'])
    lat = [r['lat'] for r in rows if r['lat'] is not None]
    per, conf = {}, {}
    for r in rows:
        e = r['expect_id']
        per.setdefault(e, [0, 0])
        per[e][1] += 1
        if r['det_id'] == e:
            per[e][0] += 1
        else:
            conf[(e, r['det_id'])] = conf.get((e, r['det_id']), 0) + 1
    wake_ok = sum(w['ok'] for w in wakes)

    L = []
    A = L.append
    A('=' * 68)
    A('语音指令识别评测报告        模型标签: %s' % args.tag)
    A('=' * 68)
    A('时间: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    A('板子自报模型: %s' % (model or '未知'))
    A('测试方式: 单条音频测试轨整段回放(试验间隔恒定 %.1f s)' % 3.0)
    A('')
    A('[A] 指令识别率(关键指标)')
    A('    识别正确 : %d / %d = %.1f%%' % (hit, n, 100.0 * hit / max(n, 1)))
    if lat:
        A('    响应延迟 : 平均 %.0f ms, 中位 %.0f ms, 最快 %d ms, 最慢 %d ms'
          % (sum(lat) / len(lat), sorted(lat)[len(lat) // 2], min(lat), max(lat)))
    A('    误驱动   : 实际触发动作 %d 次(说明唤醒门放行了这些次)' % len(acts))
    A('')
    A('[B] 唤醒词')
    A('    唤醒成功 : %d / %d = %.1f%%' % (wake_ok, len(wakes), 100.0 * wake_ok / max(len(wakes), 1)))
    A('')
    A('[B2] 完整流程(唤醒->口令 一次说对)')
    A('    完整成功 : %d / %d = %.1f%%' % (joint, len(blocks), 100.0 * joint / max(len(blocks), 1)))
    A('')
    A('[C] 抗误触发')
    A('    纯静音段 %.0f 秒内误触发 %d 次' % (meta['silence_total'], fp_quiet))
    A('    全程误唤醒(凭空说起唤醒词) %d 次' % wake_fp)
    A('')
    A('--- 各指令识别率 ---')
    for cid in sorted(per):
        h, t = per[cid]
        A('    id=%d %-6s %d/%d  %.0f%%' % (cid, CMDS.get(cid, '?'), h, t, 100.0 * h / t))
    A('')
    A('--- 混淆情况(期望 -> 实际) ---')
    if conf:
        for (e, d) in sorted(conf, key=lambda k: -conf[k]):
            A('    %-6s -> %-14s %d 次' % (CMDS.get(e, e),
                                           CMDS.get(d, d) if d else '漏检(没反应)', conf[(e, d)]))
    else:
        A('    无混淆')
    A('')
    A('--- 逐次明细 ---')
    A('    序号 期望   实际       prob  响应延迟ms')
    for i, r in enumerate(rows, 1):
        A('    %3d  %-6s %-10s %-5s %s' % (i, r['expect_word'], r['det_word'] or 'NONE',
                                           r['prob'], r['lat']))
    A('=' * 68)

    out_txt = os.path.join(HERE, 'bench_%s.txt' % args.tag)
    out_csv = os.path.join(HERE, 'bench_%s.csv' % args.tag)
    out_log = os.path.join(HERE, 'bench_%s_serial.log' % args.tag)
    with open(out_log, 'w', encoding='utf-8') as f:
        for t, txt in board.events:
            f.write('[%8.3f] %s\n' % (t - origin, txt))
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')

    with open(out_csv, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['kind', 'expect_id', 'expect_word', 'det_id', 'det_word',
                    'prob', 'latency_ms'])
        for r in rows:
            w.writerow(['cmd', r['expect_id'], r['expect_word'], r['det_id'],
                        r['det_word'], r['prob'], r['lat']])
        for r in wakes:
            w.writerow(['wake', 1, '你好小车', 1 if r['ok'] else None,
                        'ni hao xiao che' if r['ok'] else None, r['prob'], r['lat']])

    for k, v in [('板子自报模型', model or '未知'),
                 ('指令识别率', '%.1f%% (%d/%d)' % (100.0 * hit / max(n, 1), hit, n)),
                 ('响应延迟', ('%.0f ms' % (sum(lat) / len(lat))) if lat else '-'),
                 ('唤醒成功率', '%.0f%% (%d/%d)' % (100.0 * wake_ok / max(len(wakes), 1), wake_ok, len(wakes))),
                 ('完整流程', '%.0f%% (%d/%d)' % (100.0 * joint / max(len(blocks), 1), joint, len(blocks))),
                 ('静音误触发', '%d 次' % fp_quiet),
                 ('误驱动动作', '%d 次' % len(acts))]:
        print('  %s: %s' % (k, v))
    print('report: %s' % out_txt)
    return 0



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', required=True)
    ap.add_argument('--port', default='COM3',
                    help='串口(Windows 在设备管理器里看, ESP32-S3 的 USB-CDC 口)')
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--wake-rounds', type=int, default=5)
    ap.add_argument('--silence', type=int, default=5)
    ap.add_argument('--track', default='', help='用整条测试轨评测(推荐), 值为轨道 tag')
    ap.add_argument('--expect-model', default='',
                    help='板子自报模型名必须等于它, 否则中止 —— 防止烧录失败却把旧固件的结果当成新模型的成绩')
    ap.add_argument('--only', type=int, default=0, help='只测某一条指令(反复回放), 0=测全部')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--warmup', type=int, default=2, help='正式测量前的预热次数(不计入统计)')
    ap.add_argument('--quick', action='store_true', help='冒烟: 只用"停下"1 次')
    ap.add_argument('--no-silence', action='store_true')
    args = ap.parse_args()

    if not winsound:
        print('!! 需要 Windows(winsound) 才能回放')
        return 1

    random.seed(args.seed)
    print('reset board ...')
    try:
        subprocess.run([sys.executable, '-m', 'esptool', '--port', args.port,
                        '--after', 'hard_reset', 'chip-id'],
                       capture_output=True, timeout=90)
    except Exception as e:
        print('  (reset skipped: %s)' % e)
    time.sleep(1.5)

    print('open %s ...' % args.port)
    board = Board(args.port)
    model, ready = board.wait_ready()
    print('model=%s  ready=%s' % (model, ready))
    if not ready:
        print('!! 没等到就绪标志, 仍然继续(结果可能受开机过程影响)')
    if args.expect_model and model != args.expect_model:
        print('!! 板子自报模型是 %s, 期望 %s —— 中止(烧录可能没成功)' % (model, args.expect_model))
        board.close()
        return 2
    board.reset()          # 丢掉开机日志, 后面看到的每个事件都是本次实验产生的

    if args.track:
        rc = run_track(board, args.track, args, model)
        board.close()
        return rc

    if args.quick:
        plan = [8]
        args.rounds, args.wake_rounds, args.silence = 1, 1, 0
    elif args.only:
        plan = [args.only] * args.rounds
        args.wake_rounds = min(args.wake_rounds, 2)
        args.silence = 0
    else:
        plan = []
        for cid in CMDS:
            plan += [cid] * args.rounds
        random.shuffle(plan)

    t_start = time.monotonic()
    # 预热: 正式测量前先跑几下, 避免把"首次下发音频"的偶发失败算进去
    for _ in range(0 if args.quick else args.warmup):
        run_cmd_trial(board, 8, listen=2.0)
        time.sleep(0.3)
    print('=== A. 指令识别: %d 次试验 ===' % len(plan))
    rows = []
    for i, cid in enumerate(plan, 1):
        r = run_cmd_trial(board, cid)
        r['no'] = i
        r['expect_word'] = CMDS[cid]
        rows.append(r)
        print('  [%2d/%2d] %-6s -> %-10s prob=%-5s lat=%-6s %s' % (
            i, len(plan), CMDS[cid],
            r['det_word'] or 'NONE', r['prob'], r['lat_from_speech'],
            'OK' if r['det_id'] == cid else 'MISS'))

    print('=== B. 唤醒词: %d 次 ===' % args.wake_rounds)
    wakes = []
    for i in range(args.wake_rounds):
        w = run_wake_trial(board)
        wakes.append(w)
        print('  唤醒[%d] ok=%d prob=%s lat=%s' % (i + 1, w['ok'], w['prob'], w['lat_ms']))

    fp = 0
    if not args.no_silence and not args.quick:
        print('=== C. 静音误触发: %d x 5s ===' % args.silence)
        for k in range(args.silence):
            n = run_silence(board, 5.0)
            fp += n
            print('  静音[%d] 误触发 %d' % (k + 1, n))

    board.close()
    wall = time.monotonic() - t_start

    # ---------------- 统计 ----------------
    n = len(rows)
    hit = sum(1 for r in rows if r['det_id'] == r['expect_id'])
    lat = [r['lat_from_speech'] for r in rows if r['lat_from_speech'] is not None]
    wake_ok = sum(w['ok'] for w in wakes)
    acted = [r for r in rows if r['action']]

    per, conf = {}, {}
    for r in rows:
        e = r['expect_id']
        per.setdefault(e, [0, 0])
        per[e][1] += 1
        if r['det_id'] == e:
            per[e][0] += 1
        else:
            conf[(e, r['det_id'])] = conf.get((e, r['det_id']), 0) + 1

    L = []
    A = L.append
    A('=' * 68)
    A('语音指令识别评测报告        模型标签: %s' % args.tag)
    A('=' * 68)
    A('时间: %s      总耗时: %.0f 秒' % (time.strftime('%Y-%m-%d %H:%M:%S'), wall))
    A('板子自报模型: %s   (就绪: %s)' % (model or '未知', '是' if ready else '否'))
    A('')
    A('[A] 指令识别率(关键指标)')
    A('    识别正确 : %d / %d = %.1f%%' % (hit, n, 100.0 * hit / max(n, 1)))
    if lat:
        A('    响应延迟 : 平均 %.0f ms, 中位 %.0f ms, 最快 %d ms, 最慢 %d ms'
          % (sum(lat) / len(lat), sorted(lat)[len(lat) // 2], min(lat), max(lat)))
    A('    误驱动保护: 试验中实际触发动作 %d 次(应为 0, 说明唤醒门有效)' % len(acted))
    A('')
    A('[B] 唤醒词')
    A('    唤醒成功 : %d / %d = %.1f%%' % (wake_ok, len(wakes), 100.0 * wake_ok / max(len(wakes), 1)))
    if not args.no_silence and not args.quick:
        A('')
        A('[C] 静音误触发')
        A('    误触发 %d 次 / %d 秒 = %.2f 次/分钟' % (fp, args.silence * 5, fp * 60.0 / max(args.silence * 5, 1)))
    A('')
    A('--- 各指令识别率 ---')
    for cid in sorted(per):
        h, t = per[cid]
        A('    id=%d %-6s %d/%d  %.0f%%' % (cid, CMDS.get(cid, '?'), h, t, 100.0 * h / t))
    A('')
    A('--- 混淆情况(期望 -> 实际) ---')
    if conf:
        for (e, d) in sorted(conf, key=lambda k: -conf[k]):
            A('    %-6s -> %-12s %d 次' % (CMDS.get(e, e),
                                           CMDS.get(d, d) if d else '漏检(没反应)', conf[(e, d)]))
    else:
        A('    无混淆')
    A('')
    A('--- 逐次明细 ---')
    A('    序号 期望   实际       prob  词尾延迟ms 音频ms 误唤醒')
    for r in rows:
        A('    %3d  %-6s %-10s %-5s %-9s %-6s %s' % (
            r['no'], r['expect_word'], r['det_word'] or 'NONE',
            r['prob'], r['lat_from_speech'], r['audio_ms'], r.get('wake_fp', 0)))
    A('=' * 68)

    out_txt = os.path.join(HERE, 'bench_%s.txt' % args.tag)
    out_csv = os.path.join(HERE, 'bench_%s.csv' % args.tag)
    out_log = os.path.join(HERE, 'bench_%s_serial.log' % args.tag)
    with open(out_log, 'w', encoding='utf-8') as f:
        for t, txt in board.events:
            f.write('[%8.3f] %s\n' % (t - t_start, txt))
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    with open(out_csv, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['no', 'expect_id', 'expect_word', 'det_id', 'det_word', 'prob',
                    'lat_from_speech', 'lat_from_end', 'audio_ms', 'extra', 'action'])
        for r in rows:
            w.writerow([r['no'], r['expect_id'], r['expect_word'], r['det_id'], r['det_word'],
                        r['prob'], r['lat_from_speech'], r['lat_from_end'], r['audio_ms'],
                        r['extra'], r['action']])

    for k, v in [('板子自报模型', model or '未知'),
                 ('指令识别率', '%.1f%% (%d/%d)' % (100.0 * hit / max(n, 1), hit, n)),
                 ('响应延迟', ('%.0f ms' % (sum(lat) / len(lat))) if lat else '-'),
                 ('唤醒成功率', '%.0f%% (%d/%d)' % (100.0 * wake_ok / max(len(wakes), 1), wake_ok, len(wakes))),
                 ('静音误触发', '%d 次' % fp)]:
        print('  %s: %s' % (k, v))
    print('report: %s' % out_txt)
    print('csv   : %s' % out_csv)
    return 0


if __name__ == '__main__':
    sys.exit(main())
