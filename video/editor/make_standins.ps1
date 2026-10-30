# Make five stand-in "face shot" clips with Windows text-to-speech, each line said twice (two takes), so the
# auto-editor can be tested end to end before the real footage exists. Writes video/footage-test/shot1..shot5.
# Formats vary on purpose (phone-like): 1080p30 landscape, portrait 1080x1920, 4K60, and 1080p30.
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$out = Join-Path $root "video\footage-test"
New-Item -ItemType Directory -Force $out | Out-Null
$lines = @(
  "Every bank has a queue of fraud alerts. And every alert lands on a person who has minutes to decide.",
  "Rules miss the new patterns. Plain AI can't be trusted to block someone's card. And when a black box decides, nobody can check its work.",
  "So we built Kavach: an AI investigator that works like a good analyst. It gathers evidence from the bank's graph, decides by the bank's own policy, and writes the case up.",
  "Every claim it makes can be re-run against the data. Every block needs a human with the right role. And if anything fails, it stops instead of guessing.",
  "And it learns. It mined five thousand closed cases, proposed a new detector, and waited for a manager to approve it. That detector caught a ring nobody had labeled."
)
$formats = @(
  @{ size = "1920x1080"; fps = 30; color = "0x2b3a4a" },
  @{ size = "1080x1920"; fps = 30; color = "0x3a2b4a" },
  @{ size = "3840x2160"; fps = 60; color = "0x2b4a3a" },
  @{ size = "1920x1080"; fps = 30; color = "0x4a3a2b" },
  @{ size = "1920x1080"; fps = 30; color = "0x4a2b2b" }
)
for ($i = 0; $i -lt 5; $i++) {
  $n = $i + 1
  $wav = Join-Path $out "shot$n.wav"
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.Rate = 0
  $s.SetOutputToWaveFile($wav)
  $pb = New-Object System.Speech.Synthesis.PromptBuilder
  $pb.AppendBreak([TimeSpan]::FromMilliseconds(900))
  $pb.AppendText("Okay, take one.")
  $pb.AppendBreak([TimeSpan]::FromMilliseconds(1200))
  $pb.AppendText($lines[$i])
  $pb.AppendBreak([TimeSpan]::FromMilliseconds(1500))
  $pb.AppendText($lines[$i])
  $pb.AppendBreak([TimeSpan]::FromMilliseconds(1300))
  $s.Speak($pb)
  $s.Dispose()
  $f = $formats[$i]
  $mp4 = Join-Path $out ("shot$n." + ($(if ($n -eq 3) { "mov" } else { "mp4" })))
  $font = "C\:/Windows/Fonts/arialbd.ttf"
  $vf = "drawtext=fontfile='$font':text='STAND-IN SHOT $n':fontcolor=white:fontsize=h/12:x=(w-text_w)/2:y=h*0.3"
  & ffmpeg -v error -y -f lavfi -i "color=c=$($f.color):s=$($f.size):r=$($f.fps)" -i $wav -vf $vf -shortest -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -c:a aac -b:a 160k $mp4
  Remove-Item $wav
  Write-Host "made $mp4"
}
