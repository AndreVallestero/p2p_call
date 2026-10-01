# p2p_call

Minimal full-duplex P2P voice call over UDP with Opus LOWDELAY (2.5 ms frames, mono, hard CBR 98304 bps). Tkinter UI with Host / Peer modes.

## Install

### Debian / Ubuntu

```bash
sudo apt install python3-tk libportaudio2 libopus0
pip install sounddevice numpy opuslib
```

### Arch

```bash
sudo pacman -S tk portaudio opus
pip install sounddevice numpy opuslib
```

### Windows

```cmd
pip install sounddevice numpy opuslib
```

> Windows also needs `opus.dll` (libopus) on `PATH` or next to `p2p_call.py`.

## Run

```bash
python p2p_call.py
```

Headless CLI:

```bash
python p2p_call.py --mode host --port 8192 --buffer 8
python p2p_call.py --mode peer --address 192.168.1.5:8192 --buffer 8
```
