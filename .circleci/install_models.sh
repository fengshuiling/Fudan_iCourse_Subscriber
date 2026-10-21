#!/usr/bin/env bash
set -euo pipefail

# Same SenseVoice/VAD release as check.yml. Keep int8 only to reduce cache
# storage. Alternative backends require an explicit installer/cache change.
test "${ASR_BACKEND:-sensevoice}" = sensevoice || {
  echo 'This installer supports SenseVoice; configure models/cache for another backend.' >&2
  exit 1
}
model_dir="${ASR_MODEL_DIR:-.cache/icourse-models/sensevoice}"
vad_path="${SILERO_VAD_PATH:-.cache/icourse-models/silero_vad.onnx}"
mkdir -p "$model_dir" "$(dirname "$vad_path")"
release=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models
bundle=sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
scratch=$(mktemp -d)
trap 'rm -rf -- "$scratch"' EXIT

if ! test -s "$model_dir/model.int8.onnx" || ! test -s "$model_dir/tokens.txt"; then
  curl --fail --location --retry 3 --connect-timeout 30 --max-time 900 \
    "$release/$bundle.tar.bz2" -o "$scratch/model.tar.bz2"
  tar -xjf "$scratch/model.tar.bz2" -C "$scratch"
  cp "$scratch/$bundle/model.int8.onnx" "$model_dir/"
  cp "$scratch/$bundle/tokens.txt" "$model_dir/"
fi
if ! test -s "$vad_path"; then
  curl --fail --location --retry 3 --connect-timeout 30 --max-time 180 \
    "$release/silero_vad.onnx" -o "$scratch/silero_vad.onnx"
  mv "$scratch/silero_vad.onnx" "$vad_path"
fi
test -s "$model_dir/model.int8.onnx"
test -s "$model_dir/tokens.txt"
test -s "$vad_path"
echo 'SenseVoice int8 and Silero VAD ready.'
