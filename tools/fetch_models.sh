#!/bin/bash
# Download the local speech models (sherpa-onnx releases) into ./models and print their SHA-256.
set -euo pipefail
mkdir -p models; cd models; B=https://github.com/k2-fsa/sherpa-onnx/releases/download
for u in asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2 tts-models/kokoro-multi-lang-v1_0.tar.bz2 tts-models/vits-cantonese-hf-xiaomaiiwn.tar.bz2; do
  f=$(basename $u); curl -fsSL -o $f $B/$u; shasum -a 256 $f; tar xjf $f; rm $f; done
curl -fsSL -o silero_vad.onnx $B/asr-models/silero_vad.onnx; shasum -a 256 silero_vad.onnx
