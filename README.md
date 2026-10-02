# p2p_call

Minimal full-duplex P2P voice call over UDP with Opus LOWDELAY (2.5 ms frames, mono, hard CBR 98304 bps). Tkinter UI with Host / Peer modes, shared receive volume (0-200%), frame buffer and optional AES-256-GCM password (stored plaintext in `p2p_call.ini`).

## Install

### Debian / Ubuntu

```bash
sudo apt install python3-tk libportaudio2 libopus0
pip install sounddevice numpy opuslib cryptography
```

### Arch

```bash
sudo pacman -S tk portaudio opus
pip install sounddevice numpy opuslib cryptography
```

### Windows

```cmd
pip install sounddevice numpy opuslib cryptography
```

> Windows also needs 64-bit `opus.dll` (libopus) next to `p2p_call.py`
> (same folder — `p2p_call.py` picks it up automatically) or on `PATH`.
> Match the DLL to your Python: 64-bit Python needs 64-bit `opus.dll`.
> Download it here: https://github.com/ShiftMediaProject/opus/releases
> (e.g. `libopus_v1.4_msvc17.zip` — use the 64-bit `opus.dll`).
> Official builds are also listed at https://opus-codec.org/downloads/.

## Run

```bash
python p2p_call.py
```

Headless CLI:

```bash
python p2p_call.py --mode host --port 8192 --buffer 8
python p2p_call.py --mode peer --address 192.168.1.5:8192 --buffer 8
```
