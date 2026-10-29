# Bundled wake-word models

`hey_hermes.tflite` and `hey_hermes.onnx` are the on-device "Hey Hermes"
hotword model. This is the default detector for the wake word feature (see
`website/docs/user-guide/features/wake-word.md`); no training or setup is
required to say "hey hermes".

- **Engine:** [pyopen-wakeword](https://github.com/rhasspy/pyopen-wakeword)
  (rhasspy's maintained fork of openWakeWord; Apache-2.0). Runs TFLite via a
  bundled `tensorflowlite_c` library on supported hosts. Linux ARM64 uses
  [openWakeWord](https://github.com/dscripka/openWakeWord) 0.6.0 with ONNX
  Runtime because pyopen-wakeword's compatible wheel requires glibc 2.34 and
  its source archive carries an x86-64 library.
- **Provenance:** trained with the openWakeWord training pipeline (synthetic
  TTS-generated speech), which produces the `.tflite` artifact. Redistribution
  is permitted under the openWakeWord license.
- **Label:** the model registers as `hey_hermes` (matches the filename).
- **Runtime:** the `pyopen-wakeword` wheel includes the shared TFLite feature
  models. The Linux ARM64 path bundles the openWakeWord v0.5.1
  `melspectrogram.onnx` and `embedding_model.onnx` release assets (Apache-2.0;
  SHA-256 `ba2b0e0f...65eb176f` and `70d16429...075c1f`). Starting either
  engine requires no model download. Identical model files alone do not
  establish identical scores across inference engines or platforms.

To use a different phrase, point `wake_word.openwakeword.model` at an
absolute path to a compatible `.tflite` model, or an `.onnx` model on Linux
ARM64. Hermes does not download models by name. See the wake-word docs for
the training guide and platform limits.
