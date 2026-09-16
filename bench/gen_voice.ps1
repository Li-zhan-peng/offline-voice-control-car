$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

# 生成的音频与测试轨都放在脚本同级的 voice_set/ 目录下
$outDir = Join-Path $PSScriptRoot 'voice_set'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# id -> 指令文本（与固件 esp_mn_commands_add 的顺序一致）
$phrases = [ordered]@{
    'wake' = '你好小车'
    'c4'   = '前进'
    'c5'   = '后退'
    'c6'   = '左转'
    'c7'   = '右转'
    'c8'   = '停下'
    'c2'   = '开灯'
    'c3'   = '关灯'
}

$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono)

foreach ($k in $phrases.Keys) {
    $file = Join-Path $outDir "$k.wav"
    $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $synth.SelectVoice('Microsoft Huihui Desktop')
    $synth.Volume = 100
    $synth.Rate = 0            # -10..10，0 为正常语速
    $synth.SetOutputToWaveFile($file, $fmt)
    $synth.Speak($phrases[$k])
    $synth.Dispose()
    $len = (Get-Item $file).Length
    "{0,-5} {1,-6} {2,8} bytes" -f $k, $phrases[$k], $len
}
