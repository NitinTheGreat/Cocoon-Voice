# Generates local SYNTHETIC speech fixtures with the built-in Windows SAPI voice (no provider key).
# Output: tests/fixtures/audio/sapi_*.wav (16 kHz mono PCM16, git-ignored). Run from livekit-voice/:
#   powershell -ExecutionPolicy Bypass -File scripts\make_speech_fixtures.ps1
$dir = Join-Path $PSScriptRoot "..\tests\fixtures\audio"
New-Item -ItemType Directory -Force $dir | Out-Null
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$phrases = @{
  "hey_cat_question" = "Hey Cat, what should I check before starting the excavator?"
  "hey_cat_only"     = "Hey Cat."
  "background_talk"  = "Did you see the game last night? It was a close one."
}
foreach ($k in $phrases.Keys) { $s.SetOutputToWaveFile((Join-Path $dir "sapi_$k.wav"), $fmt); $s.Speak($phrases[$k]) }
$s.Dispose()
Write-Output "wrote $($phrases.Count) fixtures to $dir (voice: $((New-Object System.Speech.Synthesis.SpeechSynthesizer).Voice.Name))"
