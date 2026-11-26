# Haru-4B on a Windows GPU through speech.cpp's Vulkan build (issue #59, D127).
#
# speech.cpp (MIT, ggml) runs Haru-4B as one GGUF file with its codec: no Python, no CUDA, Vulkan
# on AMD and NVIDIA. This script fetches the pinned release into a folder of its own, checks it
# against the release's SHA256SUMS, fetches the model and measures one turn from a WAVE file, then
# prints how to try it live. Nothing is installed system-wide; delete the folder to remove it.
#
#   powershell -ExecutionPolicy Bypass -File haru_eval.ps1 [-Type q8_0] [-Question question.wav]
#
# Use headphones for --live: the model hears what speakers play.
param(
    [string]$Version = "0.11.2",
    [string]$Type = "q8_0",          # f16 (10.6 GB) needs ~11 GB free VRAM, q8_0 ~7 GB, q4_k ~5 GB (text breaks)
    [string]$Question = "",          # a WAVE file with a question; default: a silent second
    [int]$MaxSeconds = 300,          # keys and values are held for this long: ~0.3 GB per 10 minutes
    [string]$Dir = "$env:LOCALAPPDATA\topcoat-voice-eval"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force $Dir | Out-Null
Set-Location $Dir
$zip = "speech-$Version-windows-x64-vulkan.zip"
$base = "https://github.com/nyosegawa/speech.cpp/releases/download/v$Version"
if (-not (Test-Path $zip)) { Invoke-WebRequest "$base/$zip" -OutFile $zip }
Invoke-WebRequest "$base/SHA256SUMS" -OutFile SHA256SUMS
$want = (Select-String -Path SHA256SUMS -Pattern " $([regex]::Escape($zip))$").Line.Split(" ")[0]
$have = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($want -ne $have) { throw "checksum mismatch for $zip" }
Expand-Archive -Force $zip -DestinationPath bin
$speech = (Get-ChildItem bin -Recurse -Filter speech.exe | Select-Object -First 1).FullName
$env:SPEECH_HOME = "$Dir\home"
& $speech devices
& $speech pull "haru-4b:$Type"

if (-not $Question) {
    # one second of silence at 24 kHz: the model then opens the conversation by itself
    $Question = "$Dir\silence.wav"
    $n = 24000; $data = New-Object byte[] ($n * 2)
    $hdr = [System.IO.MemoryStream]::new(); $w = [System.IO.BinaryWriter]::new($hdr)
    $w.Write([Text.Encoding]::ASCII.GetBytes("RIFF")); $w.Write([int](36 + $data.Length))
    $w.Write([Text.Encoding]::ASCII.GetBytes("WAVEfmt ")); $w.Write([int]16); $w.Write([int16]1); $w.Write([int16]1)
    $w.Write([int]24000); $w.Write([int]48000); $w.Write([int16]2); $w.Write([int16]16)
    $w.Write([Text.Encoding]::ASCII.GetBytes("data")); $w.Write([int]$data.Length); $w.Write($data)
    [IO.File]::WriteAllBytes($Question, $hdr.ToArray())
}
$t = Measure-Command { & $speech talk "haru-4b:$Type" --language de --max-seconds $MaxSeconds -o reply.wav $Question | Out-Host }
Write-Host ("one turn from a file: {0:N1} s wall (load included)" -f $t.TotalSeconds)
Write-Host "Live, with headphones:"
Write-Host "  `$env:SPEECH_HOME='$Dir\home'; & '$speech' talk haru-4b:$Type --live --language de --max-seconds $MaxSeconds --name Haru --persona 'Eine freundliche Assistentin, die kurz und natürlich spricht.'"
Write-Host "A conversation that falls 1 s behind stops with 'device': then try q4_k or a smaller --max-seconds."
