#include "custom_wake_word.h"
#include "audio_service.h"
#include "system_info.h"
#include "board.h"
#include "display.h"
#include "../../boards/HiwonderExploit_S3/motor.h"
#include "../../boards/HiwonderExploit_S3/heading.h"
#include "../../boards/HiwonderExploit_S3/obstacle.h"
#include "../../boards/HiwonderExploit_S3/feedback.h"

#include <cstdio>
#include <cmath>
#include <esp_log.h>
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <esp_timer.h>


#define TAG "CustomWakeWord"

// 车头灯(摄像头旁那颗避障灯)由板级文件实现 —— 它挂在 XL9555 的 P0.0 上
extern "C" void BoardSetCameraLed(bool on);

// ── 固定角度转向 ──
// 以前"左转/右转"是"一直原地转，直到说停下"，实际用起来会一直转圈、方向也控制不准。
// 现在改成航向闭环: 转够 90 度就自动停下(用板载 IMU 积分航向, 每 20ms 修正一次)。
// 想转更多就再说一次(说两遍 = 180 度)。
static const float kTurnDegrees = 90.0f;

static void TurnTask(void* param) {
    const float deg = (float)(int)(intptr_t)param;      // 正=左转, 负=右转
    if (heading_ok()) {
        heading_turn(deg, 12, 8);                       // 快档 12 / 接近目标降到 8(0.3 秒转完太快, 慢一点更稳)
    } else {
        // IMU 不可用时退化为"定时转动", 至少不会一直转圈
        ESP_LOGW(TAG, "无 IMU, 用定时方式转向 %.0f 度", deg);
        const int8_t sp = (int8_t)(deg > 0 ? -16 : 16);
        motor_set_wheel_speeds(sp, (int8_t)(-sp));
        vTaskDelay(pdMS_TO_TICKS(1050));
        motor_stop();
    }
    vTaskDelete(NULL);
}

static void StartTurnTask(float degrees) {
    xTaskCreate(TurnTask, "turn", 4096, (void*)(intptr_t)(int)degrees, 5, NULL);
}

// ---- SAFETY: auto stop after a given time (prevents runaway car) ----
static void AutoStopTask(void* param) {
    uint32_t ms = (uint32_t)(uintptr_t)param;
    vTaskDelay(pdMS_TO_TICKS(ms));
    motor_stop();
    ESP_LOGI(TAG, "AUTO STOP (safety)");
    vTaskDelete(NULL);
}

static void StartAutoStop(uint32_t ms) {
    xTaskCreate(AutoStopTask, "auto_stop", 3072, (void*)(uintptr_t)ms, 5, NULL);
}

// ---- WAKE GATE: commands only accepted within a few seconds after the wake word ----
static int64_t g_command_window_until = 0;   // microseconds; 0 = closed

static void OpenCommandWindow(int seconds) {
    g_command_window_until = esp_timer_get_time() + (int64_t)seconds * 1000000LL;
    ESP_LOGI(TAG, ">> COMMAND WINDOW OPEN for %d s", seconds);
}

static bool CommandWindowOpen() {
    // 车正在动 -> 命令窗一直有效: 用户显然还在指挥这辆车, 中途换指令
    // (左转/后退/...)不该再喊一次唤醒词。只有停下之后才需要重新唤醒。
    // 安全性由能量门兜底: 噪声要能过能量门才会被当成运动指令。
    if (motor_is_moving()) {
        return true;
    }
    return esp_timer_get_time() < g_command_window_until;
}

static void CloseCommandWindow() {
    g_command_window_until = 0;
}

// ───────────────────────── 语音活动(能量)证据 ─────────────────────────
// 实测发现: 模型后验分不开"真口令"和"噪声误触发" ——
//    真口令的 top-1 后验最低只有 0.052, 而噪声凭空触发最高能到 0.170, 两者完全重叠。
// 所以必须引入模型之外的信息: 检测发生的那一刻, 输入信号里到底有没有"人在说话"的能量。
//
// 做法: 用慢速平均 RMS 当环境噪声底(时间常数约 6 秒, 跟不上一句话的起伏),
// 用最近约 1 秒的峰值 RMS 当语音能量, 两者之比就是"此刻有多像人在说话"的证据。
// 真口令说完时比值高, 噪声触发时比值接近 1。
static float g_noise_floor = 0.0f;   // 慢速平均 RMS = 环境噪声底
static float g_recent_peak = 0.0f;   // 最近约 1 秒的峰值 RMS = 语音能量

static void UpdateVoiceEnergy(const int16_t* pcm, size_t n) {
    if (pcm == nullptr || n == 0) return;
    double acc = 0.0;
    for (size_t i = 0; i < n; i++) {
        acc += (double)pcm[i] * (double)pcm[i];
    }
    float rms = sqrtf((float)(acc / (double)n));
    if (g_noise_floor <= 0.0f) g_noise_floor = rms;
    g_noise_floor = 0.995f * g_noise_floor + 0.005f * rms;      // tau ~ 6.4 s
    g_recent_peak = (rms > g_recent_peak) ? rms : (g_recent_peak * 0.968f);  // tau ~ 1 s
}

static float VoiceEnergyRatio() {
    if (g_noise_floor < 1.0f) return 0.0f;
    return g_recent_peak / g_noise_floor;
}

// ---- FEEDBACK: show what was heard on the LCD, so the user knows it was their voice ----
static const char* CmdName(int id) {
    switch (id) {
        case 1:  return "你好小车";
        case 2:  return "开灯";
        case 3:  return "关灯";
        case 4:  return "前进";
        case 5:  return "后退";
        case 6:  return "左转";
        case 7:  return "右转";
        case 8:  return "停下";
        default: return "未知";
    }
}

static void ShowHeard(int cmd_id, float prob) {
    auto display = Board::GetInstance().GetDisplay();
    if (display == nullptr) return;
    char buf[80];
    snprintf(buf, sizeof(buf), "听到: %s (%d%%)", CmdName(cmd_id), (int)(prob * 100.0f));
    display->ShowNotification(buf, 2000);
}

// ───────────────────────── 判决规则(精度层) ─────────────────────────
// ESP-SR 只在 prob[0] 超过一个全局门限时就吐结果, 固件原来直接执行 top-1。
// 但模型同时给了 top-5 候选和后验概率(prob[0..num-1]), 亚军信息被白白扔掉。
//
// 做法: 把全局门限压低当"召回层"(宁滥勿缺), 用下面的规则做"精度层":
//     score[i] = prob[i] + bias[命令id]        每命令偏置: 修正天生后验偏高/偏低的口令
//     接受当且仅当  score_max >= tau[id]  且  score_max - score_2nd >= kRuleMargin
// 不满足就拒识 —— 对小车来说"没听懂"远比"听错方向"安全。
//
// 下面这些参数由 PC 端 _analyze_rules.py 在实测后验数据上做 2 折交叉验证学出,
// 全部为 0 时等价于"模型说什么就执行什么"(与原始固件行为一致)。
static const float kCmdBias[9]  = {0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f};
static const float kCmdTau[9]   = {0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f, 0.0f};
static const float kRuleMargin  = 0.0f;

// 能量门限: 台式标定时 54 条 TTS 真口令的能量比最低 1.94, 7 次噪声误触发最高 2.54,
// 当时取 1.5。但真人实测发现说话更远/更轻时真口令会掉到 1.39 甚至 1.25, 被误挡。
// 回头看噪声分布: 0.42 / 0.75 / 0.95 / 1.06 / 1.08 / 1.15 / 2.54 ——
// 除 2.54 那个离群点外全部在 1.15 以下, 所以门限从 1.5 降到 1.2 挡噪声能力不变,
// 却能放进更多真口令(纯赚)。
static const float kEnergyGate  = 1.2f;

static inline float CmdBias(int id) { return (id >= 0 && id < 9) ? kCmdBias[id] : 0.0f; }
static inline float CmdTau(int id)  { return (id >= 0 && id < 9) ? kCmdTau[id] : 0.0f; }


CustomWakeWord::CustomWakeWord()
    : wake_word_pcm_(), wake_word_opus_() {
}

CustomWakeWord::~CustomWakeWord() {
    if (multinet_model_data_ != nullptr && multinet_ != nullptr) {
        multinet_->destroy(multinet_model_data_);
        multinet_model_data_ = nullptr;
    }

    if (wake_word_encode_task_stack_ != nullptr) {
        heap_caps_free(wake_word_encode_task_stack_);
    }

    if (wake_word_encode_task_buffer_ != nullptr) {
        heap_caps_free(wake_word_encode_task_buffer_);
    }

    if (models_ != nullptr) {
        esp_srmodel_deinit(models_);
    }
}

bool CustomWakeWord::Initialize(AudioCodec* codec) {
    codec_ = codec;

    models_ = esp_srmodel_init("model");
    if (models_ == nullptr || models_->num == -1) {
        ESP_LOGE(TAG, "Failed to initialize wakenet model");
        return false;
    }

    // 初始化 multinet (命令词识别)
    mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, ESP_MN_CHINESE);
    //英文唤醒词识别：
    //mn_name_ = esp_srmodel_filter(models_, ESP_MN_PREFIX, ESP_MN_ENGLISH);
    if (mn_name_ == nullptr) {
        ESP_LOGE(TAG, "Failed to initialize multinet, mn_name is nullptr");
        ESP_LOGI(TAG, "Please refer to https://pcn7cs20v8cr.feishu.cn/wiki/CpQjwQsCJiQSWSkYEvrcxcbVnwh to add custom wake word");
        return false;
    }

    ESP_LOGI(TAG, "multinet: %s", mn_name_);
    multinet_ = esp_mn_handle_from_name(mn_name_);
    multinet_model_data_ = multinet_->create(mn_name_, 3000);  // 3 秒超时
    multinet_->set_det_threshold(multinet_model_data_, CONFIG_CUSTOM_WAKE_WORD_THRESHOLD / 100.0f);
    esp_mn_commands_clear();
    esp_mn_commands_add(1, CONFIG_CUSTOM_WAKE_WORD);   // 命令1 = 唤醒词（触发云端对话）
    // ───── 以下为本地离线控制命令（不联网，直接执行动作）─────
    esp_mn_commands_add(2, "kai deng");                // 开灯
    esp_mn_commands_add(3, "guan deng");               // 关灯
    esp_mn_commands_add(4, "qian jin");                // 前进
    esp_mn_commands_add(5, "hou tui");                 // 后退
    esp_mn_commands_add(6, "zuo zhuan");               // 左转
    esp_mn_commands_add(7, "you zhuan");               // 右转
    esp_mn_commands_add(8, "ting xia");                // 停下
    // ── 停下的同义词 ──
    // 急停是唯一绝不能失效的动作, 所以给它更大的"触发面":
    // 几种说法都映射到同一个动作(命令 8)。它们之间即使互相混淆也无害 ——
    // 反正都是停车, 不存在"听错方向"的风险。这样用户不用记住唯一那句词。
    esp_mn_commands_add(8, "ting zhi");                // 停止
    esp_mn_commands_add(8, "ting che");                // 停车
    esp_mn_commands_add(8, "kuai ting");               // 快停
    esp_mn_commands_add(8, "bie dong");                // 别动
    esp_mn_commands_update();
    
    multinet_->print_active_speech_commands(multinet_model_data_);
    return true;
}

void CustomWakeWord::OnWakeWordDetected(std::function<void(const std::string& wake_word)> callback) {
    wake_word_detected_callback_ = callback;
}

void CustomWakeWord::Start() {
    running_ = true;
}

void CustomWakeWord::Stop() {
    running_ = false;
}

void CustomWakeWord::Feed(const std::vector<int16_t>& data) {
    if (multinet_model_data_ == nullptr || !running_) {
        return;
    }

    esp_mn_state_t mn_state;
    // If input channels is 2, we need to fetch the left channel data
    if (codec_->input_channels() == 2) {
        auto mono_data = std::vector<int16_t>(data.size() / 2);
        for (size_t i = 0, j = 0; i < mono_data.size(); ++i, j += 2) {
            mono_data[i] = data[j];
        }

        UpdateVoiceEnergy(mono_data.data(), mono_data.size());
        StoreWakeWordData(mono_data);
        mn_state = multinet_->detect(multinet_model_data_, const_cast<int16_t*>(mono_data.data()));
    } else {
        UpdateVoiceEnergy(data.data(), data.size());
        StoreWakeWordData(data);
        mn_state = multinet_->detect(multinet_model_data_, const_cast<int16_t*>(data.data()));
    }
    
    if (mn_state == ESP_MN_STATE_DETECTING) {
        return;
    } else if (mn_state == ESP_MN_STATE_DETECTED) {
        esp_mn_results_t *mn_result = multinet_->get_results(multinet_model_data_);
        int cmd_id = mn_result->command_id[0];
        ESP_LOGI(TAG, "=== LOCAL CMD: id=%d, word=%s, prob=%.2f ===",
                 cmd_id, mn_result->string, mn_result->prob[0]);
        // 把模型给出的 top-N 候选和后验概率全部打出来。
        // 这一行是"算法创新"的数据来源: 有了 prob[0..n-1] 和 runner-up 的身份,
        // 就可以离线研究更好的判决规则(边际/每命令门限), 而不是只用一个全局门限。
        {
            char pb[192];
            int off = snprintf(pb, sizeof(pb), "MNPOST num=%d", mn_result->num);
            for (int i = 0; i < mn_result->num && i < ESP_MN_RESULT_MAX_NUM; i++) {
                if (off >= (int)sizeof(pb) - 24) break;
                off += snprintf(pb + off, sizeof(pb) - off, " %d:%.3f",
                                mn_result->command_id[i], mn_result->prob[i]);
            }
            ESP_LOGI(TAG, "%s", pb);
        }
        // 能量证据: 这一行让 PC 端能比较"真口令"与"噪声误触发"的能量分布
        ESP_LOGI(TAG, "VOICEENERGY ratio=%.2f peak=%.1f noise=%.1f",
                 VoiceEnergyRatio(), g_recent_peak, g_noise_floor);
        ShowHeard(cmd_id, mn_result->prob[0]);   // LCD feedback: show what was heard
        // All commands are handled LOCALLY (fully offline, no cloud involved).
        // WAKE GATE: 唤醒门只用来挡"运动"命令, 防止凭空一句话就让小车跑起来。
        //
        // 这里曾经犯过一个设计错误: 所有命令(包括"停下")都要先喊唤醒词, 且只在
        // 唤醒后 20 秒内有效。结果车跑出去 20 秒后喊"停下"会被静默忽略 ——
        // 屏幕照样显示"听到: 停下"(显示发生在门检查之前!), 车却停不下来。
        // 急停命令绝不能被任何门挡住, 所以:
        //   停下(8)            -> 跳过唤醒门和能量门, 永远执行(误停是安全方向)
        //   开灯(2)/关灯(3)    -> 跳过唤醒门(只动一颗灯, 不需要先唤醒), 仍过能量门滤噪声
        //   前进/后退/左转/右转 -> 仍然要求唤醒门 + 能量门
        const bool is_stop_cmd   = (cmd_id == 8);
        const bool is_light_cmd  = (cmd_id == 2 || cmd_id == 3);

        if (cmd_id == 1) {
            // 唤醒词也要过能量门: 唤醒门一开, 后面 20 秒内任何口令都会被放行,
            // 所以"凭空唤醒"比"凭空报一条口令"更危险。
            if (VoiceEnergyRatio() < kEnergyGate) {
                ESP_LOGI(TAG, "RULE: wake REJECTED by energy gate (ratio=%.2f)",
                         VoiceEnergyRatio());
                feedback_refuse();
            } else {
                OpenCommandWindow(20);
                feedback_awake();     // 白闪: 告诉用户"我在听"
            }
        } else if (!is_stop_cmd && !is_light_cmd && !CommandWindowOpen()) {
            ESP_LOGI(TAG, "cmd %d ignored (wake word was not said)", cmd_id);
            auto display = Board::GetInstance().GetDisplay();
            if (display != nullptr) display->ShowNotification("请先说: 你好小车", 1500);
            feedback_refuse();
        } else {
            // ── 精度层: 后验规则 + 能量门, 没把握就拒识, 而不是硬猜一条执行 ──
            int rule_id = cmd_id;
            float best = -1e9f, second = -1e9f;
            for (int i = 0; i < mn_result->num && i < ESP_MN_RESULT_MAX_NUM; i++) {
                float sc = mn_result->prob[i] + CmdBias(mn_result->command_id[i]);
                if (sc > best) {
                    second = best;
                    best = sc;
                    rule_id = mn_result->command_id[i];
                } else if (sc > second) {
                    second = sc;
                }
            }
            float ratio = VoiceEnergyRatio();
            bool reject = false;
            if (best < CmdTau(rule_id)) reject = true;
            if (second > -1e8f && (best - second) < kRuleMargin) reject = true;
            // 能量门作用于【除"停下"以外的所有命令】。
            //
            // 这里改过两版, 记录一下结论:
            //   第一版: 只挡运动命令 —— 开灯/关灯放行, 理由是"灯误触发代价小"。
            //           结果噪声一响灯就自己开, 用户反馈"动不动就自己打开了"。这个判断是错的。
            //   第二版(现在): 除了急停, 全部要过能量门。
            //
            // 为什么必须靠能量门而不能靠阈值: 日志里那次噪声触发的"开灯"置信度高达 0.37,
            // 比很多真口令还高, 用阈值根本分不开; 但它的能量比只有 0.49(明显不是人在说话)。
            // 这正是本项目判决算法的核心结论 —— 后验概率分不开, 能量证据才分得开。
            //   停下(8): 永远放行 —— 误停是安全方向, 且急停必须绝对可靠
            if (cmd_id != 8 && ratio < kEnergyGate) reject = true;

            // 灯具防抖: 两条开关灯指令之间至少间隔 1.5 秒。
            // 低触发门限下同一句话可能被重复识别, 表现为"喊一次灯闪两下/又开又关"。
            static int64_t s_last_light_us = 0;
            if (!reject && is_light_cmd) {
                const int64_t now = esp_timer_get_time();
                if (now - s_last_light_us < 1500000LL) {
                    ESP_LOGI(TAG, "灯指令被防抖忽略(距上次 %.2f 秒)",
                             (now - s_last_light_us) / 1e6);
                    reject = true;
                } else {
                    s_last_light_us = now;
                }
            }

            if (reject) {
                ESP_LOGI(TAG, "RULE: REJECT id=%d (best=%.3f second=%.3f ratio=%.2f) -> no action",
                         rule_id, best, second, ratio);
                auto display = Board::GetInstance().GetDisplay();
                if (display != nullptr) display->ShowNotification("没听清,请再说一遍", 1500);
                feedback_refuse();     // 黄闪: 远处也能看到"它没听懂"
            } else {
            feedback_accept();         // 绿闪: 远处也能看到"它听懂了并执行了"
            // 小车一直走, 直到说"停下"; 只保留 60 秒兜底的自动停机。
            switch (rule_id) {
                case 4:  // qian jin  -> forward
                    heading_abort_turn();             // 新指令优先: 取消还在跑的转向
                    // ── 意图服从现实 ──
                    // 语音说的是"人的意图", 超声波给的是"物理现实"。
                    // 前方 25cm 内有东西就拒绝执行 —— 具身智能最基本的安全原则。
                    if (!obstacle_allow_forward()) {
                        ESP_LOGW(TAG, "ACTION: forward REFUSED (前方 %u mm 有障碍)",
                                 obstacle_distance_mm());
                        auto d4 = Board::GetInstance().GetDisplay();
                        if (d4 != nullptr) d4->ShowNotification("前方有障碍,不能前进", 1500);
                        feedback_refuse();
                        break;
                    }
#if !CONFIG_VOICE_CONTROL_NO_MOTOR
                    motor_set_wheel_speeds(15, 15);   // 15: 室内速度(用户反馈 25 太快, 惯性刹不住)
#endif
                    ESP_LOGI(TAG, "ACTION: forward (until 'stop')");
                    StartAutoStop(60000);
                    break;
                case 5:  // hou tui   -> backward
                    heading_abort_turn();
#if !CONFIG_VOICE_CONTROL_NO_MOTOR
                    motor_set_wheel_speeds(-15, -15);
#endif
                    ESP_LOGI(TAG, "ACTION: backward (until 'stop')");
                    StartAutoStop(60000);
                    break;
                case 6:  // zuo zhuan -> 原地左转 90 度后自动停下
                {
                    heading_abort_turn();   // 取消上一个可能还在跑的转向
                    ESP_LOGI(TAG, "ACTION: turn left %.0f deg", kTurnDegrees);
                    StartTurnTask(+kTurnDegrees);
                    break;
                }
                case 7:  // you zhuan -> 原地右转 90 度后自动停下
                {
                    heading_abort_turn();
                    ESP_LOGI(TAG, "ACTION: turn right %.0f deg", kTurnDegrees);
                    StartTurnTask(-kTurnDegrees);
                    break;
                }
                case 8:  // ting xia / ting zhi / ting che / kuai ting / bie dong -> stop
                    // 关键: 必须先中止可能在跑的转向任务!
                    // 转向跑在独立任务里(含精修最长约 3.8 秒), 只调 motor_stop() 的话,
                    // 转向循环下一轮就把速度又下发回去了 —— 表现为"喊停没反应"。
                    heading_abort_turn();
                    motor_stop();
                    CloseCommandWindow();             // 车停了, 命令窗立即关闭
                    ESP_LOGI(TAG, "ACTION: stop (含转向中止; 命令窗已关, 下次要动需先唤醒)");
                    break;
                case 2:  // kai deng -> 点亮车头灯(摄像头旁那颗避障灯)
                {
                    BoardSetCameraLed(true);
                    auto d2 = Board::GetInstance().GetDisplay();
                    if (d2 != nullptr) d2->ShowNotification("车头灯已开", 1200);
                    ESP_LOGI(TAG, "ACTION: head lamp ON");
                    break;
                }
                case 3:  // guan deng -> 熄灭车头灯
                {
                    BoardSetCameraLed(false);
                    auto d3 = Board::GetInstance().GetDisplay();
                    if (d3 != nullptr) d3->ShowNotification("车头灯已关", 1200);
                    ESP_LOGI(TAG, "ACTION: head lamp OFF");
                    break;
                }
                default:
                    ESP_LOGI(TAG, "cmd %d has no action defined", rule_id);
                    break;
            }
            }
        }
        multinet_->clean(multinet_model_data_);
    } else if (mn_state == ESP_MN_STATE_TIMEOUT) {
        ESP_LOGD(TAG, "Command word detection timeout, cleaning state");
        multinet_->clean(multinet_model_data_);
    }
}

size_t CustomWakeWord::GetFeedSize() {
    if (multinet_model_data_ == nullptr) {
        return 0;
    }
    return multinet_->get_samp_chunksize(multinet_model_data_);
}

void CustomWakeWord::StoreWakeWordData(const std::vector<int16_t>& data) {
    // store audio data to wake_word_pcm_
    wake_word_pcm_.push_back(data);
    // keep about 2 seconds of data, detect duration is 30ms (sample_rate == 16000, chunksize == 512)
    while (wake_word_pcm_.size() > 2000 / 30) {
        wake_word_pcm_.pop_front();
    }
}

void CustomWakeWord::EncodeWakeWordData() {
    const size_t stack_size = 4096 * 7;
    wake_word_opus_.clear();
    if (wake_word_encode_task_stack_ == nullptr) {
        wake_word_encode_task_stack_ = (StackType_t*)heap_caps_malloc(stack_size, MALLOC_CAP_SPIRAM);
        assert(wake_word_encode_task_stack_ != nullptr);
    }
    if (wake_word_encode_task_buffer_ == nullptr) {
        wake_word_encode_task_buffer_ = (StaticTask_t*)heap_caps_malloc(sizeof(StaticTask_t), MALLOC_CAP_INTERNAL);
        assert(wake_word_encode_task_buffer_ != nullptr);
    }

    wake_word_encode_task_ = xTaskCreateStatic([](void* arg) {
        auto this_ = (CustomWakeWord*)arg;
        {
            auto start_time = esp_timer_get_time();
            auto encoder = std::make_unique<OpusEncoderWrapper>(16000, 1, OPUS_FRAME_DURATION_MS);
            encoder->SetComplexity(0); // 0 is the fastest

            int packets = 0;
            for (auto& pcm: this_->wake_word_pcm_) {
                encoder->Encode(std::move(pcm), [this_](std::vector<uint8_t>&& opus) {
                    std::lock_guard<std::mutex> lock(this_->wake_word_mutex_);
                    this_->wake_word_opus_.emplace_back(std::move(opus));
                    this_->wake_word_cv_.notify_all();
                });
                packets++;
            }
            this_->wake_word_pcm_.clear();

            auto end_time = esp_timer_get_time();
            ESP_LOGI(TAG, "Encode wake word opus %d packets in %ld ms", packets, (long)((end_time - start_time) / 1000));

            std::lock_guard<std::mutex> lock(this_->wake_word_mutex_);
            this_->wake_word_opus_.push_back(std::vector<uint8_t>());
            this_->wake_word_cv_.notify_all();
        }
        vTaskDelete(NULL);
    }, "encode_wake_word", stack_size, this, 2, wake_word_encode_task_stack_, wake_word_encode_task_buffer_);
}

bool CustomWakeWord::GetWakeWordOpus(std::vector<uint8_t>& opus) {
    std::unique_lock<std::mutex> lock(wake_word_mutex_);
    wake_word_cv_.wait(lock, [this]() {
        return !wake_word_opus_.empty();
    });
    opus.swap(wake_word_opus_.front());
    wake_word_opus_.pop_front();
    return !opus.empty();
}
