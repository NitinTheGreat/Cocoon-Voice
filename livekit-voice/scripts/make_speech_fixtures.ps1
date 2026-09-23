# Generates local SYNTHETIC speech fixtures with the built-in Windows SAPI voice (no provider key).
# Output: tests/fixtures/audio/sapi_*.wav (16 kHz mono PCM16, git-ignored). Run from livekit-voice/:
#   powershell -ExecutionPolicy Bypass -File scripts\make_speech_fixtures.ps1
$dir = Join-Path $PSScriptRoot "..\tests\fixtures\audio"
New-Item -ItemType Directory -Force $dir | Out-Null
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$phrases = @{
  "hey_cat_question"      = "Hey Cat, what should I check before starting the excavator?"
  "hey_cat_only"          = "Hey Cat."
  "background_talk"       = "Did you see the game last night? It was a close one."
  "hey_livekit_question"  = "Hey LiveKit, what is the track tension?"
  "hey_livekit_only"      = "Hey LiveKit."
  "near_hey_liquid"       = "Hey liquid, pour it."
  "near_live_kit"         = "The live kit is ready."
  # live probe (scripts/live_probe.py)
  "hey_cat_long_request"  = "Hey Cat, explain step by step how to do a full pre-start walkaround inspection on an excavator, in detail."
  "long_request"          = "Explain step by step how to do a full pre-start walkaround inspection on an excavator, in detail."
  "mm_hmm"                = "Mm-hmm."
  "okay"                  = "Okay."
  "stop"                  = "Stop."
  "wait"                  = "Wait."
  "yes"                   = "Yes."
  "okay_but_correction"   = "Okay, but I meant the other machine, the wheel loader."
  "question_part1"        = "Can you tell me"
  "question_part2"        = "what I should do next?"
}
foreach ($k in $phrases.Keys) { $s.SetOutputToWaveFile((Join-Path $dir "sapi_$k.wav"), $fmt); $s.Speak($phrases[$k]) }
$s.Dispose()
Write-Output "wrote $($phrases.Count) fixtures to $dir"
