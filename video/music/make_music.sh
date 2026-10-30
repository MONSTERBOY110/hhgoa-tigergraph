#!/usr/bin/env bash
# Generate three calm music beds for the launch video with Google Lyria RealTime (Gemini key from .env, never printed).
# Usage: bash video/music/make_music.sh   (writes video/music/bed_a.wav, bed_b.wav, bed_c.wav, about 80 s each)
set -u
cd "$(dirname "$0")/../.."
PY="${MEDIA_PY:-python}"
RECIPE="$HOME/.claude/skills/media-use/audio/scripts/lyria-recipe.py"
GEMINI_API_KEY="$(grep -E '^GEMINI_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '\r"'"'"' ')"
export GEMINI_API_KEY
[ -n "$GEMINI_API_KEY" ] || { echo "no GEMINI_API_KEY in .env"; exit 1; }
gen() {
  local name="$1" bpm="$2" bright="$3" dens="$4" prompt="$5"
  echo "== $name: $prompt (bpm $bpm)"
  "$PY" "$RECIPE" --output "video/music/$name.wav" --duration 80 --bpm "$bpm" --brightness "$bright" --density "$dens" --scale MAJOR \
    --prompt "$prompt" --negative-prompt "vocals, singing, harsh distortion, aggressive drums, dubstep, heavy metal" 2>&1 | tail -3
}
gen bed_a 80 0.55 0.35 "Soothing ambient felt piano with warm analog pads, gentle and hopeful, slow, cinematic, clean mix, no drums"
gen bed_b 84 0.60 0.45 "Calm lofi chillhop, mellow electric piano, soft brushed drums, warm round bass, relaxed and optimistic"
gen bed_c 90 0.65 0.40 "Gentle inspiring cinematic underscore, soft strings, light marimba plucks, warm pads, minimal percussion"
ls -la video/music/*.wav
