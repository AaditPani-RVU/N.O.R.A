# Training NORA's wake word

`hey_nora.yaml` is the recipe for a custom openWakeWord model that fires on
**"hey nora"** and **"hi nora"**. Everything here is build tooling — it runs
once, produces a `.onnx`, and NORA loads that file at startup. None of it is
imported at runtime.

## Why there is a README at all

The upstream instructions cannot be followed literally any more, and each of
the places they break costs an hour to diagnose. In order:

1. **`train.py` is not in the pip wheel.** `pip install openwakeword` gives you
   inference only. Training lives in the GitHub repo, which you have to clone
   separately.
2. **`pip install openwakeword` installs the wrong version on Python 3.12+.**
   0.5.x declares a hard `tflite-runtime` dependency on Linux, that has no
   wheel past 3.11, so the resolver silently walks back to 0.4.0 — whose
   `Model()` signature is different enough to raise `TypeError` inside
   NORA's detector thread. Install with `--no-deps` (see below and
   `requirements.txt`).
3. **TensorFlow is not actually required**, despite the notebook installing
   `tensorflow-cpu==2.8.1`, `tensorflow_probability` and `onnx_tf` — none of
   which install on a modern Python. They are imported *inside*
   `convert_onnx_to_tflite()` and only run under `--convert_to_tflite`. NORA
   uses the onnx backend, so skip all three.
4. **There are two `piper-sample-generator` repos and only one works.**
   `rhasspy/piper-sample-generator` has been restructured into a package, but
   `train.py` does `from generate_samples import generate_samples` — a
   top-level module. Use **`dscripka/piper-sample-generator`**, the fork the
   config comments point at, which still has that layout. It also depends on
   `espeak-phonemizer` (a ctypes wrapper over system libespeak-ng) rather than
   `piper-phonemize`, which has no wheel past cp312.
5. **The notebook's background-audio download 404s.** AudioSet was
   restructured from `data/bal_train09.tar` into `data/bal_train/NN.parquet`.
   `prepare_data.py` in this directory handles the current layout.

## Setup

Training runs in its own venv outside this repo. Every dependency does have a
3.13 wheel, but this stack pins 2023-era versions and NORA's runtime venv has
no reason to carry a toolchain it never imports.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p ~/nora-wakeword-training && cd ~/nora-wakeword-training
uv venv --python 3.12 .venv

git clone https://github.com/dscripka/openWakeWord.git
git clone https://github.com/dscripka/piper-sample-generator.git
wget -O piper-sample-generator/models/en_US-libritts_r-medium.pt \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/en_US-libritts_r-medium.pt
```

Torch must match the **driver**, not the newest release. `nvidia-smi` reports
the maximum CUDA version the installed driver supports; a torch built for
anything higher fails with "The NVIDIA driver on your system is too old" and
silently falls back to CPU, which turns sample generation from minutes into
hours. Driver 550 caps at CUDA 12.4:

```sh
uv pip install --python .venv/bin/python torch torchaudio \
  --index-url https://download.pytorch.org/whl/cu124
uv pip install --python .venv/bin/python \
  torchinfo torchmetrics speechbrain audiomentations torch-audiomentations \
  acoustics mutagen pronouncing deep-phonemizer datasets scipy tqdm pyyaml \
  onnx soundfile pyarrow espeak-phonemizer webrtcvad
uv pip install --python .venv/bin/python -e ./openWakeWord --no-deps
```

Verify the GPU is actually visible before going further — this is the single
check that saves the most time:

```sh
.venv/bin/python -c "import torch; print(torch.cuda.is_available())"   # must print True
```

## Data

```sh
# ~17.3 GB of precomputed negative features + an 0.2 GB validation set
curl -L -C - -O https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy
curl -L -C - -O https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy

# room impulse responses + background noise (resumable, idempotent)
python /path/to/JARVIS/training/wakeword/prepare_data.py --dir ./data
```

The 17 GB file is the one that buys the low false-positive rate — it is 2000
hours of negative audio, already reduced to features. There is no smaller
published version.

## Run it

**Pilot first.** A wrong path in the config surfaces at the *training* stage,
which is hours after generation starts. Prove the pipeline end to end with a
throwaway run before spending a night on it:

```sh
cp /path/to/JARVIS/training/wakeword/hey_nora.yaml ./pilot.yaml
sed -i 's/^n_samples: .*/n_samples: 500/; s/^n_samples_val: .*/n_samples_val: 500/; s/^steps: .*/steps: 5000/' pilot.yaml

.venv/bin/python openWakeWord/openwakeword/train.py --training_config pilot.yaml --generate_clips
.venv/bin/python openWakeWord/openwakeword/train.py --training_config pilot.yaml --augment_clips
.venv/bin/python openWakeWord/openwakeword/train.py --training_config pilot.yaml --train_model
```

If that produces `output/hey_nora.onnx`, the pipeline is sound. Then run the
same three commands against `hey_nora.yaml` for the real model.

## Install the result

```sh
mkdir -p /path/to/JARVIS/models/wakeword
cp output/hey_nora.onnx /path/to/JARVIS/models/wakeword/
```

Then point `config.yaml` at it — paths may be relative to the project root:

```yaml
wakeword:
  enabled: true
  model: "models/wakeword/hey_nora.onnx"
  sensitivity: 0.5
```

Tune `sensitivity` against your own room rather than trusting the default.
Higher means fewer false triggers and more repeated wake words; the number that
matters is how often it fires while you are talking to somebody else.
