# -*- coding: utf-8 -*-
"""把 voice_set 里的 WAV 统一归一化到同一峰值, 保证各口令音量一致(公平比较的前提)"""
import wave
import array
import os
import glob

TARGET_PEAK = 30000     # 约 -0.8 dBFS
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'voice_set')

for p in sorted(glob.glob(os.path.join(SRC, '*.wav'))):
    w = wave.open(p, 'rb')
    params, n = w.getparams(), w.getnframes()
    raw = w.readframes(n)
    w.close()

    a = array.array('h')
    a.frombytes(raw)
    peak = max(max(a), -min(a)) or 1
    g = TARGET_PEAK / float(peak)
    for i in range(len(a)):
        v = int(a[i] * g)
        a[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)

    out = p  # 直接覆盖
    w = wave.open(out, 'wb')
    w.setparams(params)
    w.writeframes(a.tobytes())
    w.close()
    print('%-10s peak %6d -> %6d  (gain %.2fx)' % (os.path.basename(p), peak, TARGET_PEAK, g))
