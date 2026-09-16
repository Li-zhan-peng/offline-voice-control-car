# -*- coding: utf-8 -*-
"""
抓一段串口日志, 只挑出"为什么没动作"相关的行。

要回答的问题:
  屏幕显示"听到: 前进"之后, 固件到底走了哪条分支?
    A. cmd N ignored (wake word was not said)  -> 唤醒门没开(没先说唤醒词, 或唤醒词被挡)
    B. RULE: REJECT ...                        -> 被判决规则拒识(能量门)
    C. ACTION: forward                         -> 固件执行了, 那就是电机/供电的问题
用法: python _capture.py 180
"""
import os
import sys
import time
import serial

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 180
PORT = sys.argv[2] if len(sys.argv) > 2 else 'COM3'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_capture.log')

KEYS = ('LOCAL CMD', 'ignored', 'RULE:', 'ACTION:', 'VOICEENERGY',
        'WINDOW OPEN', 'AUTO STOP', 'motor', 'MOTOR', 'MOTOR_INIT')

s = None
for attempt in range(12):
    try:
        s = serial.Serial(PORT, 115200, timeout=0.3)
        break
    except Exception as e:
        print('open attempt %d failed: %s' % (attempt + 1, e))
        time.sleep(1)
if s is None:
    print('ERR: cannot open port')
    sys.exit(1)

print('串口已打开 %s, 抓 %d 秒 ... 请开始说话' % (PORT, SECONDS))
t0 = time.monotonic()
buf = b''
lines = []
reconnects = 0
while time.monotonic() - t0 < SECONDS:
    try:
        d = s.read(4096)
    except Exception:
        # 板子的 USB-CDC 在复位时会重新枚举, 旧句柄随即失效。
        # 以前这里只是打印错误继续读, 结果整段日志全丢 —— 现在自动重连。
        reconnects += 1
        try:
            s.close()
        except Exception:
            pass
        time.sleep(1.5)
        s = None
        for _ in range(10):
            try:
                s = serial.Serial(PORT, 115200, timeout=0.3)
                print('  [串口重连成功, 第 %d 次]' % reconnects)
                break
            except Exception:
                time.sleep(1)
        if s is None:
            print('  [串口重连失败, 继续等待]')
        continue
    if not d:
        continue
    buf += d
    while b'\n' in buf:
        raw, buf = buf.split(b'\n', 1)
        t = time.monotonic() - t0
        txt = raw.decode('utf-8', 'ignore').strip()
        if not txt:
            continue
        lines.append('[%6.1f] %s' % (t, txt))

try:
    s.close()
except Exception:
    pass

with open(OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(lines) + '\n')

key = [l for l in lines if any(k in l for k in KEYS)]
print('')
print('=== 关键行 (%d 条 / 总 %d 行) ===' % (len(key), len(lines)))
for l in key:
    print(l)
print('')
print('完整日志: %s' % OUT)
