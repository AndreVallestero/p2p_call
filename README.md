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

> Windows also needs 64-bit `opus.dll` (libopus) next to `p2p_call.py`
> (same folder — `p2p_call.py` picks it up automatically) or on `PATH`.
> Match the DLL to your Python: 64-bit Python needs 64-bit `opus.dll`.
> Download it here: https://github.com/ShiftMediaProject/opus/releases
> (e.g. `libopus_v1.4_msvc17.zip` — use the 64-bit `opus.dll`).
> Official builds are also listed at https://opus-codec.org/downloads/.

## Windows troubleshooting: "could not find the module 'opus.dll'"

If the path in the error is correct and the file IS there, a *dependency*
of `opus.dll` is missing — not the file itself. In order:

1. Copy **every** `.dll` from the downloaded zip next to `p2p_call.py`,
   not just `opus.dll` (MinGW builds need their libgcc/libwinpthread siblings).
2. Install the Microsoft C++ Redistributable (MSVC builds need it):
   https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist
3. Check 64-bit Python ↔ 64-bit DLL match:
   `python -c "import struct; print(struct.calcsize('P')*8)"` must match the DLL.
4. Optionally inspect `opus.dll` with Dependencies
   (https://github.com/lucasg/Dependencies) to see the missing DLL.

## Run

```bash
python p2p_call.py
```

Headless CLI:

```bash
python p2p_call.py --mode host --port 8192 --buffer 8
python p2p_call.py --mode peer --address 192.168.1.5:8192 --buffer 8
```
