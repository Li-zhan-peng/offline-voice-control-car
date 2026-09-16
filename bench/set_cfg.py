# -*- coding: utf-8 -*-
"""
安全地修改 sdkconfig（必须用 Python 改，不能用 PowerShell）。

踩过的坑：Windows PowerShell 5.1 的 Get-Content 默认按 ANSI(GBK) 读文件，
把 UTF-8 的中文（比如 CONFIG_CUSTOM_WAKE_WORD_DISPLAY="你好小车"）读成乱码，
再用 Set-Content 写回 UTF-8，字节就坏了；而 esp-sr 的 movemodel.py 会用
系统默认编码读 sdkconfig，读到坏字节直接 UnicodeDecodeError，编译就挂。

用法:
  python _set_cfg.py model 7            # 切到 mn7_cn
  python _set_cfg.py set CONFIG_CUSTOM_WAKE_WORD_THRESHOLD 15
  python _set_cfg.py toggley CONFIG_USE_AFE_WAKE_WORD n
  python _set_cfg.py check              # 体检：编码 + 关键项
"""
import os
import re
import sys

# ⚠ 这里改成你自己的固件工程根目录(就是含 sdkconfig 的那一层)
# 也可以用环境变量 XIAOZHI_PROJ 覆盖
PROJ = os.environ.get('XIAOZHI_PROJ', r'C:\path\to\your\firmware-project')
PATH = os.path.join(PROJ, 'sdkconfig')

MODEL_KEYS = {
    '5': 'CONFIG_SR_MN_CN_MULTINET5_RECOGNITION_QUANT8',
    '6': 'CONFIG_SR_MN_CN_MULTINET6_QUANT',
    '7': 'CONFIG_SR_MN_CN_MULTINET7_QUANT',
    '6ac': 'CONFIG_SR_MN_CN_MULTINET6_AC_QUANT',
    '7ac': 'CONFIG_SR_MN_CN_MULTINET7_AC_QUANT',
}


def read_lines():
    with open(PATH, 'rb') as f:
        raw = f.read()
    try:
        txt = raw.decode('utf-8')
    except UnicodeDecodeError as e:
        raise SystemExit('sdkconfig 不是合法 UTF-8，先修编码: %s' % e)
    if txt.startswith('\ufeff'):
        txt = txt[1:]
    return txt.split('\n')


def write_lines(lines):
    with open(PATH, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines))


def set_key(lines, key, value):
    """value='n' / '# ... is not set' 都可以"""
    out = []
    hit = False
    for ln in lines:
        if re.match(r'^(#\s+)?' + re.escape(key) + r'(=| is not set)', ln):
            hit = True
            out.append('# %s is not set' % key if value in ('n', 'no', 'notset')
                       else '%s=%s' % (key, value))
        else:
            out.append(ln)
    if not hit:
        out.append('# %s is not set' % key if value in ('n', 'no', 'notset')
                   else '%s=%s' % (key, value))
    return out


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    lines = read_lines()

    if cmd == 'model':
        want = sys.argv[2]
        for k, key in MODEL_KEYS.items():
            lines = set_key(lines, key, 'y' if k == want else 'n')
        write_lines(lines)
        for ln in lines:
            if re.match(r'^(#\s+)?CONFIG_SR_MN_CN_MULTINET[567]', ln):
                print(ln)

    elif cmd == 'set':
        key, val = sys.argv[2], sys.argv[3]
        write_lines(set_key(lines, key, val))
        print([l for l in read_lines() if key in l][:2])

    elif cmd == 'sanitize':
        # 把非 ASCII 字符换掉, 彻底避免 esp-sr 脚本按 GBK 读时炸掉
        bad = sum(1 for ln in lines for c in ln if ord(c) > 127)
        lines = [''.join(c if ord(c) < 128 else '?' for c in ln) for ln in lines]
        write_lines(lines)
        print('replaced %d non-ascii chars' % bad)

    elif cmd == 'check':
        txt = '\n'.join(lines)
        bad = [c for c in txt if ord(c) > 127]
        print('non-ascii chars: %d  %s' % (len(bad), ''.join(bad[:20])))
        for key in list(MODEL_KEYS.values()) + [
                'CONFIG_CUSTOM_WAKE_WORD', 'CONFIG_CUSTOM_WAKE_WORD_DISPLAY',
                'CONFIG_CUSTOM_WAKE_WORD_THRESHOLD', 'CONFIG_USE_CUSTOM_WAKE_WORD',
                'CONFIG_USE_AFE_WAKE_WORD', 'CONFIG_VOICE_CONTROL_NO_MOTOR',
                'CONFIG_BOARD_TYPE_HiwonderExploit_S3']:
            hits = [l for l in lines if l.startswith(key + '=') or l.startswith('# ' + key)]
            print('%-46s %s' % (key, hits[0] if hits else '(缺)'))
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == '__main__':
    sys.exit(main())
