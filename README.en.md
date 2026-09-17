# Offline Voice-Controlled Robot Car — from "hearing you right" to "never causing harm"

> 🇨🇳 中文版: [README.md](README.md)

[![ESP32-S3](https://img.shields.io/badge/MCU-ESP32--S3-blue)]()
[![ESP-SR](https://img.shields.io/badge/Speech-ESP--SR%20MultiNet-green)]()
[![Offline](https://img.shields.io/badge/Network-fully%20offline-orange)]()
[![License](https://img.shields.io/badge/License-MIT-lightgrey)]()
[![Claim reproducibility](https://github.com/Li-zhan-peng/offline-voice-control-car/actions/workflows/validate.yml/badge.svg)](https://github.com/Li-zhan-peng/offline-voice-control-car/actions/workflows/validate.yml)

**An on-device Chinese speech-command stack for environments that are offline, hands-busy, and safety-critical**
(hospital wards, nursing homes, warehouses) — plus a reproducible evaluation harness and a four-layer safety design.

> **This project does not train a speech model.** It tackles a different, equally critical problem that is
> usually skipped: **what the system should do when a small on-device model mishears, misses, or is unsure.**

**The CI badge above is not decoration.** It recomputes every claim made in this document straight from the
raw measurements archived in `data/` (92.9% → 100% recall, ~450 ms latency, the posterior-overlap result,
the 86% noise rejection, …). If a number can't be reproduced, the build fails.
**You don't have to trust our prose — just check whether the badge is green.**

---

## The problem

"Offline voice control" is usually framed as *"swap the cloud ASR for an on-device model."* In practice,
three problems show up in order:

| Problem | Common approach | What we did |
|---|---|---|
| **Can't measure it reliably** — change the room, the speaker or the volume and the conclusion changes | Shout a few commands, report a rough success rate | Built a **reproducible evaluation harness**: one continuous audio test track + serial-timestamp alignment, turning impressions into data |
| **Can't tell speech from noise** — the model emits a command when the room is noisy | Raise the confidence threshold, which also kills valid commands | Proved from measurements that the **posterior probability cannot separate real commands from noise**, then switched to an **audio-energy evidence** rule |
| **Won't stop / moves unexpectedly** | Parameter tuning and luck | A **four-layer safety design**: intent → reality → execution → fallback |

> **Others build capable robots. We build robots that don't cause harm.**

---

## Key results

All figures below are recomputed from the raw data in `data/` (see `bench/validate_claims.py`).

| Metric | Result | Note |
|---|---|---|
| Command-word recall | **92.9% → 100%** | Lowered the trigger threshold to 0.05 as a "recall layer" |
| Noise false triggers | **86% blocked while keeping 100% of real commands** | The "precision layer" (energy evidence); a posterior threshold achieves only 14% under the same constraint |
| End-to-end latency | **≈ 0.45 s** | Fully offline, no network round trip |
| Speech model size | **2.83 MB** | Runs on a 16 MB flash device (best of three model generations, 26% smaller than the previous one) |
| Motor drive success rate | **36% → 100%** | Fault tolerance: bus down-clocking + command heartbeat + driver watchdog |
| Closed-loop turn accuracy | **residual ≤ 3°** | IMU heading loop + post-stop inertia correction |

---

## How the decision rule came about

### Step 1 — the intuitive approach, refuted by our own data

The natural idea: reject a detection when the model's **posterior probability** (and the margin between
top-1 and top-2) is low.

| Observation | Value |
|---|---|
| Posterior of correct commands | mean 0.178, **minimum 0.051** |
| Posterior of spurious noise triggers | mean 0.081, **maximum 0.141** |
| Margin feature | every detection reports `num=1` — **there is no runner-up at all** |

**The lowest real command is below the highest noise trigger — the two distributions overlap completely.**
The posterior route is a dead end.

### Step 2 — switching to a physical quantity outside the model

Since the model's output can't separate them, we use information from the hardware signal chain:
**was there actually speech energy in the microphone at the moment of detection?**

```
energy evidence = peak RMS over the last 1 s  ÷  slow-averaged noise floor over 6 s
```

Real commands land in **1.94–9.94**; noise in **0.42–2.54** — largely separated, with a small overlap
(the weakest real command at 1.94 sits slightly below the loudest noise trigger at 2.54).

**The meaningful comparison is at equal constraints.** If you require *100% of real commands to be kept*,
a posterior threshold blocks only 14% of the noise, while the energy evidence blocks **86%**.
That is why one works and the other doesn't. The threshold K = 1.2 follows from a cost function that
weights a wrong action 3× higher than a missed command.

![Energy evidence](assets/figures/fig4_energy_separation.png)

### Step 3 — a two-layer decision

```
Global trigger threshold 0.05   ← recall layer: report everything worth reporting (92.9% → 100%)
        ↓
On-device decision rule          ← precision layer:
   score[i] = prob[i] + bias[command]
   accept iff
     score_max >= tau[command]
     score_max - score_2nd >= margin
     ratio >= 1.2                  (energy gate — blocks noise-triggered detections)
   otherwise reject: show "didn't catch that" on screen and do not move the motors
```

Cost: one multiply-add per sample every 32 ms (512-point RMS) — **no extra memory, no added latency.**

---

## Four-layer safety design

| Layer | Goal | Mechanism | Measured |
|---|---|---|---|
| **Intent** | Hear it right | Cross-generation model evaluation + two-layer decision | 100% recall |
| **Intent (emergency)** | Stop when told | 5 synonyms for "stop", bypasses every gate, aborts an in-progress turn | Still worked 36 s into a drive |
| **Reality** | Don't hit people | Three-stage ultrasonic policy: slow down at 70 cm → stop at 18 cm → refuse "forward" when blocked | Hand in front of the car stops it instantly |
| **Execution** | Make actions land | Speed command resent every 200 ms + rebuild the driver board when "target set but wheels not moving" | 36% → 100% |
| **Fallback** | Total loss of control | Long timeout auto-stop | — |

**Design rule: a new safety channel must reuse the *same* stop path.**
We learned this the hard way — turns run in a separate task, so a `motor_stop()` call alone gets
overwritten by the next loop iteration, which looks exactly like "the stop command doesn't work".
The ultrasonic e-stop and the voice e-stop therefore share `heading_abort_turn() + motor_stop()`.

---

## Quick start

### Reproduce the algorithm experiments (no hardware needed, ~20 min)

```bash
pip install pyserial matplotlib
cd bench
powershell -ExecutionPolicy Bypass -File gen_voice.ps1   # generate fixed command WAVs via Windows TTS
python normalize_voice.py                                # normalize to a common peak
python make_track.py std8 8 7 block                      # build one long test track + cue timeline
python bench_tts.py --tag myrun --track std8 --port COM3 # replay + serial capture + report
python analyze_rules.py --tag myrun --track std8 --write # posterior/energy analysis, threshold sweep
python validate_claims.py                                # recompute every claim in this README
python ../tools/make_figures.py                          # regenerate all figures
```

> Replaying one continuous track instead of playing each command separately matters more than it sounds:
> when files are played individually, the audio device powers down between them and swallows the first
> syllable — the same audio measured 38.1% vs 71.4% depending on the method.

### Reproduce the full prototype

See [`docs/00-reproduce.md`](docs/00-reproduce.md) for the hardware list, wiring notes, firmware integration
and a safety-behaviour checklist.

---

## Repository layout

```
├── firmware/main/    Files we modified or added (the rest belongs to upstream xiaozhi-esp32)
├── bench/            The reproducible evaluation harness (the core tooling of this project)
│   ├── bench_tts.py          single-track replay + serial timestamp alignment + reporting
│   ├── make_track.py         build audio test tracks (block / flow / wake layouts)
│   ├── analyze_rules.py      posterior & energy analysis, threshold sweep, 2-fold CV
│   ├── validate_claims.py    recompute every claim from raw data (used by CI)
│   ├── capture.py            serial log capture with automatic reconnection
│   └── set_cfg.py            safely edit sdkconfig (see the pitfalls below)
├── tools/            Figure / logo generators
├── docs/             Experiment log, hardware debugging log, scenario-driven module review
├── data/             Raw measurements (CSVs, per-run reports, filtered serial logs)
└── assets/           Figures and logo
```

---

## Pitfalls we hit (saving you a few days)

All of these are documented with log evidence in `docs/02-hardware-debugging.md`.

| Symptom | What was really going on |
|---|---|
| **"The log says the command was sent, but the car doesn't move"** | The log line was printed *before* the I²C write and the return value was never checked — it proved nothing. Action logs must be printed *after* the action and carry its result |
| **Motor drive works only 1 time out of 3** | The I²C link is unreliable at 400 kHz on this wiring (garbage reads, dropped commands). Dropping to 100 kHz made it 8/8 |
| **"I asked for a 90° turn and it turned 150°"** | The loop stopped at the moment its gyro reading hit 90°, but the chassis kept rotating by inertia. The log recorded the *instant of stopping*, not the *settled* angle — **when measurement and observation disagree, first question *which moment* you measured, not your calibration coefficients** |
| **Gyro bias self-correction locked itself out** | The "is it stationary" test used the *bias-corrected* rate, so a 12 dps bias error could never enter that branch. The stationarity test must use the **raw** value |
| **Obstacle avoidance also blocked turning** | The rule was too coarse: an in-place turn doesn't change position, so a front sensor shouldn't constrain it. Classify motion type first |
| **The "refuse to start" distance must exceed the "stop while driving" distance** | Otherwise the two rules fight each other and the car twitches in place |
| **Safety thresholds can't be judged by "looks far enough"** | The first version stopped at 20 cm, which looks generous for a 20 cm car — but `braking distance = detection latency × speed + response delay × speed + coasting ≈ 25 cm`. It was guaranteed to hit. Fix: 25→15 speed, start slowing at 70 cm, stop at 18 cm |
| **Editing `sdkconfig` with PowerShell broke the build** | Windows PowerShell 5.1 reads files as GBK by default, mangling the UTF-8 Chinese text and crashing esp-sr's build script. Always edit it with Python (`bench/set_cfg.py`) |

---

## What we deliberately did **not** do

- **No model training.** Command recognition uses Espressif's pretrained ESP-SR MultiNet models
  (mn6/mn7). Our contribution is the model *selection study* and the *decision rule*.
  If you're looking for custom wake-word training, see Espressif's ESP-SR toolchain instead.
- **No vision and no LiDAR.** This project focuses on the reliability of offline voice control;
  perception consists of a single ultrasonic module plus the on-board IMU.
- **No production validation.** This is a lab-verified prototype, not a certified product.
  Project stage and the path to productisation are described in `docs/03-functionality-and-scenarios.md`.

---

## License and attribution

This repository contains **only the files we modified or added**. Other parts depend on:

| Source | Note |
|---|---|
| [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) | Base for the board adaptation layer and application framework (MIT) |
| [ESP-SR](https://github.com/espressif/esp-sr) (Espressif) | Offline speech recognition (MultiNet); models are **not** included here |
| Hiwonder | The peripheral drivers under `firmware/main/boards/HiwonderExploit_S3/` (`motor.c/h`, `qmi8658.c/h`, `ultrasound.c/h`, `xl9555.c/h`) are adapted from vendor example code — verify their licensing before redistribution |

`bench/` and `docs/` are fully independent of vendor code and can be used on their own.
Our own code is released under the MIT License — see [LICENSE](LICENSE).

---

## Citation

```bibtex
@misc{offline_voice_control_car,
  title  = {Offline Voice-Controlled Robot Car: A Reproducible Evaluation Harness and a Four-Layer Safety Design for On-Device Speech Commands},
  year   = {2026},
  note   = {https://github.com/Li-zhan-peng/offline-voice-control-car}
}
```
