# 复现指南

两条路线，可以只选一条：

- **路线 A（推荐先做）**：只复现**算法实验**。不需要硬件，一台 Windows 电脑就能跑出本项目的核心结论。
- **路线 B**：复现**完整样机**。需要 ESP32-S3 小车 + 超声波模块。

---

## 路线 A：复现算法实验（无需硬件，约 20 分钟）

### 依赖

```bash
pip install pyserial matplotlib
```

### 步骤

```bash
cd bench

# 1) 生成固定口令音频（用 Windows 内置中文 TTS，保证每次发音完全一致）
powershell -ExecutionPolicy Bypass -File gen_voice.ps1

# 2) 归一化到同一峰值 —— 否则各口令音量不同，识别率差异会被音量污染
python normalize_voice.py

# 3) 拼装测试轨：21 次口令 + 5 次唤醒词 + 20 秒纯静音（含精确时间点 JSON）
python make_track.py std8 8 7 block

# 4) 采集与评测（需要板子接在 COM3，按实际改 --port）
python bench_tts.py --tag myrun --track std8 --port COM3

# 5) 判决规则分析：后验分布 / 能量证据 / 门限扫描 / 2 折交叉验证
python analyze_rules.py --tag myrun --track std8 --write

# 6) 生成图表
python make_figures.py
```

### 你会得到什么

- `bench_<tag>.txt`：识别率、响应延迟、各指令识别率、混淆情况、逐次明细
- `bench_<tag>.csv`：逐次原始数据
- `bench_<tag>_serial.log`：板子串口原始输出（含后验与能量证据）
- `规则优化_<tag>.txt`：后验分布、能量证据分离度、门限扫描表
- `figures/*.png|svg`：可直接放进报告或 PPT 的图表

### 只想看结论？

本仓库 `data/` 里已存档三份实测结果，不用跑也能核对：
`bench_mn7_deployed.txt`（部署后验证）、`bench_mn7_cn_lowthr.txt`（低门限召回层）、
`data/logs/*.log`（串口原始日志，已过滤为只保留本项目相关行）。

---

## 路线 B：复现完整样机

### 硬件清单

| 部件 | 说明 |
|---|---|
| 主控 | ESP32-S3 开发板（8MB PSRAM，16MB Flash），板载麦克风与扬声器 |
| 底盘 | 两轮差速小车 + 编码器电机 + I²C 电机驱动板（地址 0x34）|
| 姿态 | 板载六轴惯性测量单元 QMI8658（I²C 0x6A）|
| 感知 | 车头超声波模块（I²C 0x77，自带双 RGB 灯）|
| 扩展 | XL9555（I²C 0x20）——本项目用它的 P0.0 控制车头灯 |
| 显示 | SPI LCD（用于显示"听到: xx"与拒绝原因）|

**接线要点**：

- 惯性测量单元、超声波、XL9555、电机驱动板**挂同一条 I²C 总线**（本项目用 SDA=38 / SCL=48）
- 该总线上有五个设备，**I²C 速率必须降到 100kHz**（400kHz 实测不可靠，见 `docs/02-hardware-debugging.md`）
- 车头灯的极性需要实测确认（本项目为高电平点亮），在 `config.h` 里用 `CAMERA_LED_ACTIVE_LOW` 切换

### 固件集成

1. 准备一份可编译的 [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) 工程（ESP-IDF v5.4）
2. 把 `firmware/main/` 下的文件覆盖到工程对应路径
3. 按你的板子修改 `firmware/main/boards/HiwonderExploit_S3/config.h` 的引脚定义
4. 用 Python 改配置（**不要用 PowerShell 文本替换**，原因见 README 的"踩坑"）：

```bash
python bench/set_cfg.py model 7                                          # 选 MultiNet7
python bench/set_cfg.py set CONFIG_CUSTOM_WAKE_WORD_THRESHOLD 5          # 召回层：门限 0.05
```

5. 编译烧录：

```bash
idf.py build
idf.py -p COM3 flash monitor
```

### 上电自检

固件在开机后会延迟打印自检信息（因为开机前几秒的串口日志经常抓不到），看到下面这些就说明各模块正常：

```
motor:    === MOTOR SELF-TEST (t+8s) ===
motor:      i2c write addr=0x34 : ESP_OK
motor:      battery = 8xxx mV
heading:  === 航向自检 (t+9s) ===
heading:    IMU   : OK (QMI8658 已初始化)
obstacle: === 超声波自检 (t+10s) ===
obstacle:   模块: OK (0x77)
obstacle:   距离: xxxx mm
```

### 验证安全行为

| 操作 | 预期 |
|---|---|
| 说"你好小车" | 车头灯白闪，屏幕显示"听到: 你好小车" |
| 说"前进" | 绿灯闪，小车前进 |
| 直接说"前进"（不唤醒） | 黄闪，屏幕提示"请先说: 你好小车" |
| 手伸到车头前约 15cm | 立即停车 |
| 前方 20cm 内放障碍物后说"前进" | 拒绝执行，屏幕提示"前方有障碍,不能前进" |
| 说"左转" | 原地转约 90 度后自动停下 |
| 安静环境下静置 | 不应有误触发（能量门会挡掉噪声）|

---

## 常见问题

**Q: 串口报 `PermissionError` 或读不到数据？**
A: 十有八九是板子的 USB-CDC 在复位时重新枚举了。`bench/capture.py` 已内置自动重连；烧录脚本里遇到时等 2~3 秒重试即可。

**Q: 电机收到指令却不转？**
A: 先确认 I²C 速率是 100kHz；再确认电池电压（固件自检会打印）；最后看日志里有没有 `wheel speeds ... i2c=OK` —— 注意**这行不代表电机真的转了**，真正的证据是紧随其后的 `motion check: wheels TURNED/DID NOT turn`。

**Q: 转向角度不准？**
A: 依次检查：① 陀螺零偏是否标定成功（自检会打印零偏值）；② 陀螺量程换算是否与 `CTRL3` 寄存器一致（本项目 `CTRL3=0x63` 对应 ±1024dps，即 32 LSB/dps）；③ 是否有"停车后惯性滑转"未被修正（本项目的做法是停车后延时重测 + 小步蠕行精修）。

**Q: 说停下车不停？**
A: 「停下」在本项目里不受唤醒门与能量门限制，并能中止正在执行的转向。若仍无效，检查是否有其他任务在往电机下发速度（本项目所有安全通道共用 `heading_abort_turn() + motor_stop()` 这一条路径）。

**Q: 想换成其他语言/其他口令？**
A: 改 `custom_wake_word.cc` 里 `esp_mn_commands_add()` 的拼音串即可（同一个命令 ID 可以绑定多个同义说法，本项目给"停下"绑了 5 个）。
