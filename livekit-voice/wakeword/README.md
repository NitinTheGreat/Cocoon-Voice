# Training the "Hey Cat" wake word (livekit-wakeword)

`WAKE_MODE=livekit_wakeword` runs LiveKit's open-source wake-word classifier on the operator's room audio. It uses the Apache-2.0 package, with no account and no access key. The worker already works with *any* livekit-wakeword `.onnx`. What is missing is a model trained on "hey cat". Nothing here fakes one.

Training is a one-off job, separate from the voice worker:
1. Generate thousands of synthetic "hey cat" clips and near-miss phrases.
2. Add noise and reverb.
3. Train a small classifier.
4. Export it to ONNX.

Upstream documents training on Linux or macOS (it needs `espeak-ng`), so use **Google Colab** or **WSL2 Ubuntu**. The worker itself stays on native Windows.

## Option A: Google Colab (free T4 GPU; simplest)

1. Open https://colab.research.google.com, create a notebook, and choose Runtime → Change runtime type → **T4 GPU**.
2. Upload `livekit-voice/wakeword/hey_cat.yaml` (Files panel → upload).
3. Run:

```python
!apt-get -qq install -y espeak-ng libsndfile1 ffmpeg sox > /dev/null
!pip -q install "livekit-wakeword[train,eval,export]==0.2.1"
# Full data (~16 GB of ACAV100M negatives; best false-accept behaviour). Takes a while.
!livekit-wakeword setup --config hey_cat.yaml
# Quicker, rougher alternative:  !livekit-wakeword setup --config hey_cat.yaml --skip-acav
!livekit-wakeword run hey_cat.yaml
!livekit-wakeword eval hey_cat.yaml   # prints recall / false positives per hour; note the suggested threshold
```

4. Download `output/hey_cat/hey_cat.onnx`.

## Option B: your RTX 3050 through WSL2 Ubuntu

```bash
sudo apt install -y espeak-ng libsndfile1 ffmpeg sox portaudio19-dev python3.11-venv
python3.11 -m venv ~/lkww && source ~/lkww/bin/activate
pip install "livekit-wakeword[train,eval,export]==0.2.1"   # installs CUDA PyTorch on Linux
cd /mnt/f/Caterpillar\ hackathon/Cocoon-Voice/livekit-voice/wakeword
livekit-wakeword setup --config hey_cat.yaml --skip-acav   # add the full ACAV download if disk allows
livekit-wakeword run hey_cat.yaml
```

`tts_batch_size: 16` in `hey_cat.yaml` is sized for 4 GB of VRAM. Upstream does not publish training times; expect hours rather than minutes.

## Use the model

```powershell
Copy-Item output\hey_cat\hey_cat.onnx ..\keywords\hey_cat.onnx     # keywords/*.onnx is git-ignored
```

In `livekit-voice/.env`:

```
WAKE_MODE=livekit_wakeword
LIVEKIT_WAKEWORD_MODEL_PATH=keywords/hey_cat.onnx
LIVEKIT_WAKEWORD_THRESHOLD=0.7     # use the threshold suggested by `livekit-wakeword eval`
```

Then run `python -m cocoon_voice.doctor`. The `wakeword` line should PASS and show the inference time per call.

## Test the acoustic path before "Hey Cat" exists

LiveKit's example model is included as a test fixture:
- Set `LIVEKIT_WAKEWORD_MODEL_PATH=tests/fixtures/wakeword/hey_livekit.onnx`.
- Set `WAKE_PHRASE=Hey LiveKit`.
- Say "Hey LiveKit, …" in the Playground.

Offline measurements on synthetic Windows SAPI speech (one voice, one clip each, so only indicative):
- "Hey LiveKit." scored 0.91 and "Hey LiveKit, what is the track tension?" 0.76.
- "The live kit is ready." scored 0.60; "hey liquid" 0.08; "Hey Cat…" 0.04; background talk 0.02.

Hence the default threshold is 0.7, not upstream's example of 0.5. Inference took about 58 ms per call, so the worker runs it on a separate thread every 160 ms.

## Licence notes

- livekit-wakeword code and the exported classifier are Apache-2.0.
- The synthetic training voices come from Piper's LibriTTS VITS checkpoint, downloaded by `setup`.
- ACAV100M features are research data.

Check that these fit your hackathon and any product use.
