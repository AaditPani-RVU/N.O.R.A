# Training NORA's wake word

`hey_nora.yaml` is the recipe for a custom openWakeWord model that fires on
**"hey nora"** and **"hi nora"**. Everything here is build tooling — it runs
once, produces a `.onnx`, and NORA loads that file at startup. None of it is
imported at runtime.

Verified end to end on Debian 13 / Python 3.12 / GTX 1650, driver 550.

## Upstream is stale — read this before following any other guide

The published notebook cannot be followed literally any more. Every item below
was hit and fixed during a real run; each one costs an hour to diagnose cold.

1. **`train.py` ships only in the GitHub repo**, not the pip wheel. Cloning is
   mandatory; `pip install openwakeword` gives you inference only.
2. **`pip install openwakeword` installs the wrong version on Python 3.12+.**
   0.5.x hard-depends on `tflite-runtime`, which has no wheel past 3.11, so the
   resolver silently falls back to 0.4.0 — whose `Model()` signature differs
   enough to raise `TypeError` inside NORA's detector thread. Use `--no-deps`.
3. **TensorFlow is never needed.** The notebook installs `tensorflow-cpu==2.8.1`,
   `tensorflow_probability` and `onnx_tf`, none of which install on a modern
   Python. They are only used by `convert_onnx_to_tflite()`, and NORA uses the
   onnx backend.
4. **The tflite conversion runs even when you don't ask for it.** `train.py`
   declares its flags as `action="store_true", default="False"` — the *string*
   `"False"`, which is truthy — so `if args.convert_to_tflite:` is always true.
   The run therefore always ends in `ModuleNotFoundError: No module named
   'onnx_tf'`. **This is harmless: the `.onnx` is written before it.** Check for
   `output/<model_name>.onnx` rather than trusting the exit code.
5. **There are two `piper-sample-generator` repos and only one works.**
   `rhasspy/…` has been restructured into a package; `train.py` does
   `from generate_samples import generate_samples`, a top-level module. Use
   **`dscripka/piper-sample-generator`**.
6. **The TTS checkpoint is `en-us-libritts-high.pt` from rhasspy's v1.0.0
   release** — not the `en_US-libritts_r-medium.pt` from v2.0.0 that the newer
   notebook references. `train.py` never passes `model=`, so it takes the
   fork's hardcoded default filename and nothing else will do.
7. **`torch.load` needs `weights_only=False`.** Torch 2.6 flipped that default,
   and the checkpoint contains a pickled `SynthesizerTrn`. One-line patch below.
8. **The editable clone has no feature models.** `resources/models/` is empty in
   git; the wheel ships them. Copy them in or nothing can compute features.
9. **`acoustics` breaks on scipy ≥ 1.17**, which removed `scipy.special.sph_harm`.
   Pin `scipy<1.17`.
10. **The notebook's background-audio download 404s.** AudioSet moved from
    `data/bal_train09.tar` to `data/bal_train/NN.parquet`. `prepare_data.py`
    handles the current layout.
11. **Augmentation silently skips when features already exist**, logging only
    `WARNING: Openwakeword features already exist, skipping data augmentation`
    and exiting 0. If your pilot used the same `model_name`, the full run then
    trains on the *pilot's* clips and reports success. This is the most
    dangerous item on the list, because nothing about the output looks wrong.
    Pass `--overwrite` on the augment stage, or give the pilot its own
    `model_name`. Sanity check: augmentation of 60k clips takes tens of
    minutes, so a three-second augment stage means it did nothing.

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
curl -L -o piper-sample-generator/models/en-us-libritts-high.pt \
  https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt

# (7) torch 2.6 changed the torch.load default; the checkpoint is a pickled model
sed -i 's/torch\.load(model_path)/torch.load(model_path, weights_only=False)/' \
  piper-sample-generator/generate_samples.py
```

Torch must match the **driver**, not the newest release. `nvidia-smi` reports the
maximum CUDA version the driver supports; a torch built for anything higher
fails with "The NVIDIA driver on your system is too old" and falls back to CPU,
which turns sample generation from minutes into hours. Driver 550 caps at 12.4:

```sh
V=.venv/bin/python
uv pip install --python $V torch torchaudio --index-url https://download.pytorch.org/whl/cu124
uv pip install --python $V "scipy<1.17" numpy tqdm pyyaml onnx soundfile pyarrow \
  torchinfo torchmetrics speechbrain audiomentations torch-audiomentations \
  acoustics mutagen pronouncing deep-phonemizer datasets espeak-phonemizer webrtcvad
uv pip install --python $V -e ./openWakeWord --no-deps
uv pip install --python $V onnxruntime scikit-learn requests   # --no-deps skipped these

# (8) the editable clone ships no feature models
mkdir -p openWakeWord/openwakeword/resources/models
cp /path/to/JARVIS/.venv/lib/python3.*/site-packages/openwakeword/resources/models/*.onnx \
   openWakeWord/openwakeword/resources/models/
```

The single check that saves the most time — it must print `True`:

```sh
.venv/bin/python -c "import torch; print(torch.cuda.is_available())"
```

## Data

```sh
B=https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main
curl -L -C - -o data/openwakeword_features_ACAV100M_2000_hrs_16bit.npy \
  $B/openwakeword_features_ACAV100M_2000_hrs_16bit.npy      # 17.3 GB
curl -L -C - -o data/validation_set_features.npy $B/validation_set_features.npy

python /path/to/JARVIS/training/wakeword/prepare_data.py --dir ./data --shards 4
```

The 17 GB file is what buys the low false-positive rate — 2000 hours of negative
audio, already reduced to features. There is no smaller published version.
`prepare_data.py` is resumable and skips shards it has already decoded, so
raising `--shards` later costs only the new ones.

## Run it

Three stages, in order. **Run the pilot first** — a wrong path surfaces at the
*training* stage, hours after generation starts:

```sh
cp /path/to/JARVIS/training/wakeword/hey_nora.yaml ./pilot.yaml
sed -i 's/^n_samples: .*/n_samples: 500/; s/^n_samples_val: .*/n_samples_val: 500/;
        s/^steps: .*/steps: 5000/; s/^model_name: .*/model_name: "pilot"/' pilot.yaml

for stage in generate_clips augment_clips train_model; do
  .venv/bin/python openWakeWord/openwakeword/train.py --training_config pilot.yaml --$stage
done
```

The `model_name` override is the important part — it keeps the pilot's clips and
features in their own directory. Sharing one with the real run is how you end up
training on 500 samples and never being told (item 11).

A pilot ends with `Recall: 0.0` and an `onnx_tf` traceback. **Both are expected.**
500 samples is far too few to learn anything — the pilot proves the plumbing, not
the model. Success is the file existing:

```sh
ls output/hey_nora.onnx
```

Then the real run against `hey_nora.yaml`. Pass `--overwrite` on the augment
stage so a previous run's features cannot shadow this one:

```sh
P=openWakeWord/openwakeword/train.py
.venv/bin/python $P --training_config hey_nora.yaml --generate_clips
.venv/bin/python $P --training_config hey_nora.yaml --augment_clips --overwrite
.venv/bin/python $P --training_config hey_nora.yaml --train_model
```

Budget several hours; generation dominates — 30,000 samples took ~62 minutes on
a GTX 1650.

## Install the result

```sh
mkdir -p /path/to/JARVIS/models/wakeword
cp output/hey_nora.onnx /path/to/JARVIS/models/wakeword/
```

Point `config.yaml` at it — paths may be relative to the project root:

```yaml
wakeword:
  enabled: true
  model: "models/wakeword/hey_nora.onnx"
  sensitivity: 0.5
```

Tune `sensitivity` against your own room rather than trusting the default.
Higher means fewer false triggers and more repeated wake words; the number that
matters is how often it fires while you are talking to somebody else.
