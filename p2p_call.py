#!/usr/bin/env python3
"""p2p_call.py — minimal full-duplex P2P voice call over UDP with Opus LOWDELAY.

Requirements (Windows + Linux, minimal deps):
    pip install sounddevice numpy opuslib
    # Linux may also need: sudo apt install python3-tk libportaudio2 libopus0
    # Windows needs opus.dll (libopus) somewhere on PATH or next to this file.

Spec:
  - UI: tkinter with 2 modes (Host tab / Peer tab).
    Host: port (default 8192), frame buffer (default 8), start/stop.
    Peer: address + port (default 8192), frame buffer (default 8), start/stop.
  - Opus: APPLICATION_RESTRICTED_LOWDELAY, 2.5 ms frames (120 samples @48kHz),
    mono, hard CBR (VBR=0), bitrate 98304.
  - Network: 1 Opus frame per UDP packet, sent immediately (tiny SO_SNDBUF,
    one sendto per frame, no send queue). Packet = BE uint32 seq + opus payload.
    Receiver skips out-of-order frames whose playback time has passed.
  - Audio: full-duplex (mic + speaker simultaneously), 48 kHz mono int16,
    PortAudio 'low' latency, blocksize 120 with resampling-free FIFO so any
    host blocksize still yields exact 120-sample Opus frames.

Frame buffer semantics: jitter-buffer depth in Opus frames.
  e.g. 8 frames * 2.5 ms = 20 ms of jitter absorption.

Packet format:
  +----------------+------------------+
  | seq BE uint32  | opus payload ... |
  +----------------+------------------+
  seq wraps modulo 2**32.
"""

from __future__ import annotations

import argparse
import collections
import configparser
import ctypes
import ctypes.util
import os
import queue
import socket
import struct
import sys
import threading
import time
from pathlib import Path


_OPUS_DLL_NAMES = ("opus.dll", "libopus.dll", "libopus-0.dll")
_opus_dll_patched = False


def _ensure_opus_dll() -> str | None:
    """Make opuslib's ``find_library('opus')`` work on Windows.

    opuslib locates libopus via ``ctypes.util.find_library('opus')``, which
    searches PATH / system dirs but NOT the script's directory (Python 3.8+
    removed CWD/script-dir from DLL search). So ``opus.dll`` sitting next to
    ``p2p_call.py`` was ignored.

    This puts the script dir (plus CWD / interpreter dir / PyInstaller
    bundle dir) on the DLL search path and patches ``ctypes.util.find_library``
    to resolve ``'opus'`` to the local DLL. Must run BEFORE ``import opuslib``.
    Returns the DLL path or None. Harmless on Linux (system libopus is used).

    NOTE: a *bare* filename from the system search (e.g. ``opus.dll`` with no
    directory) is NOT trusted on its own: on Windows ``ctypes.CDLL`` with a
    bare name does not search the script dir / CWD, so opuslib would fail with
    "Could not find module 'opus.dll'" even though ``find_library`` claimed to
    find it. We always resolve to a full path when a matching file exists.
    """
    global _opus_dll_patched
    orig_find = ctypes.util.find_library
    try:
        sys_loc = orig_find("opus")
    except Exception:
        sys_loc = None

    search_dirs: list[str] = []
    try:
        search_dirs.append(str(Path(__file__).resolve().parent))
    except Exception:
        pass
    try:
        search_dirs.append(os.getcwd())
    except Exception:
        pass
    try:
        search_dirs.append(str(Path(sys.executable).resolve().parent))
    except Exception:
        pass
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        search_dirs.append(str(bundle))

    # 1) Always register search dirs (needed on Win 3.8+ for the DLL *and*
    #    its own dependencies), even if the system search already resolves.
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if d not in os.environ.get("PATH", ""):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass

    # 2) Prefer a full path to an actual file over a bare filename.
    found: str | None = None
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for name in _OPUS_DLL_NAMES:
            cand = os.path.join(d, name)
            if os.path.isfile(cand):
                found = cand
                break
        if found:
            break
    if found is None and sys_loc:
        if os.path.isabs(sys_loc) and os.path.isfile(sys_loc):
            found = sys_loc
        elif sys_loc and not os.path.dirname(sys_loc):
            # Bare filename: resolve it against PATH to a full path so the
            # loader does not depend on CWD-sensitive search semantics.
            for pdir in os.environ.get("PATH", "").split(os.pathsep):
                cand = os.path.join(pdir.strip('"'), sys_loc)
                if pdir and os.path.isfile(cand):
                    found = cand
                    break
        else:
            found = sys_loc

    # 3) Patch find_library so opuslib (imported AFTER this) resolves 'opus'
    #    to the full-path DLL.
    if found and not _opus_dll_patched:
        try:
            def _patched_find(name, _orig=orig_find, _found=found):
                try:
                    if str(name).lower() in ("opus", "libopus", "opus.dll",
                                             "libopus.dll", "libopus-0.dll"):
                        return _found
                    r = _orig(name)
                    return r if r else (_found if str(name).lower() == "opus" else None)
                except Exception:
                    return _found
            ctypes.util.find_library = _patched_find  # type: ignore[method-assign]
            _opus_dll_patched = True
        except Exception:
            pass
    return found or sys_loc


# Run at import time so even `import opuslib` after `import p2p_call` benefits.
_ensure_opus_dll()

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_MS = 2.5
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)  # 120 samples
BITRATE = 98304
SEQ_STRUCT = struct.Struct("!I")
SEQ_MASK = 0xFFFFFFFF
SEQ_HALF = 0x80000000
MAX_OPUS_BYTES = 4000  # plenty for 2.5 ms @ 98304 bps (~31 B typical)
DEFAULT_PORT = 8192
DEFAULT_BUFFER = 8
DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_VOLUME = 100  # percent (0-200)

# AES-256-GCM transport. Nonce = prefix + packet seq (never repeats per key,
# zero wire overhead); seq bytes are also the AAD so spliced packets fail auth.
GCM_TAG_LEN = 16
GCM_NONCE_PREFIX = b"p2p-call"  # 8 bytes -> 12-byte nonce with BE uint32 seq
KDF_SALT = b"p2p_call-opus-v1"
KDF_ITERATIONS = 100000

CONFIG_SECTION = "p2p_call"


def derive_opus_key(password: str) -> bytes:
    """PBKDF2-HMAC-SHA256(password) -> 32-byte AES key. Raises RuntimeError."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as ex:
        raise RuntimeError(
            "Encryption needs the 'cryptography' package. "
            "Run: pip install cryptography"
        ) from ex
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=KDF_SALT, iterations=KDF_ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def gcm_nonce(seq: int) -> bytes:
    return GCM_NONCE_PREFIX + SEQ_STRUCT.pack(seq & SEQ_MASK)


def _config_path() -> Path:
    try:
        return Path(__file__).resolve().parent / "p2p_call.ini"
    except Exception:
        return Path("p2p_call.ini")


def _get_valid_int(cp: configparser.ConfigParser, key: str, lo: int, hi: int) -> int | None:
    try:
        v = cp.getint(CONFIG_SECTION, key, fallback=-1)
    except ValueError:
        return None
    return v if lo <= v <= hi else None


def load_config() -> dict:
    """Read p2p_call.ini (whole-UI snapshot). Never raises.

    Returns {} when the file is missing or invalid; values are range-checked.
    """
    cfg: dict = {}
    cp = configparser.ConfigParser()
    try:
        if not cp.read(_config_path(), encoding="utf-8"):
            return cfg
        if not cp.has_section(CONFIG_SECTION):
            return cfg
        mode = cp.get(CONFIG_SECTION, "mode", fallback="").strip().lower()
        if mode in ("host", "peer"):
            cfg["mode"] = mode
        for key in ("host_port", "peer_port", "port"):
            v = _get_valid_int(cp, key, 1, 65535)
            if v is not None:
                cfg[key] = v
        # Back-compat: old files stored a single "port" for the active tab.
        if "port" in cfg:
            cfg.setdefault("host_port" if cfg.get("mode") != "peer" else "peer_port", cfg.pop("port"))
        v = _get_valid_int(cp, "buffer", 1, 64)
        if v is not None:
            cfg["buffer"] = v
        v = _get_valid_int(cp, "volume", 0, 200)
        if v is not None:
            cfg["volume"] = v
        for key in ("address", "peer_address", "password"):
            s = cp.get(CONFIG_SECTION, key, fallback="").strip()
            if s:
                cfg[key] = s
        # Back-compat: old files stored the peer address as "address".
        if "address" in cfg:
            cfg.setdefault("peer_address", cfg.pop("address"))
    except Exception:
        return {}
    return cfg


def save_config(mode: str, host_port: int, peer_address: str, peer_port: int,
                buffer: int, volume: int, password: str = "") -> bool:
    """Persist the whole-UI snapshot. Returns False on write failure."""
    cp = configparser.ConfigParser()
    cp[CONFIG_SECTION] = {
        "mode": mode,
        "host_port": str(host_port),
        "peer_address": peer_address,
        "peer_port": str(peer_port),
        "buffer": str(buffer),
        "volume": str(volume),
        "password": password,
    }
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            cp.write(f)
        return True
    except OSError:
        return False

# --------------------------------------------------------------------------
# Seq helpers (uint32 wrap-aware)
# --------------------------------------------------------------------------

def seq_diff(a: int, b: int) -> int:
    """How much newer `a` is than `b` (mod 2**32). 0 == equal."""
    return (a - b) & SEQ_MASK


def is_newer(a: int, b: int) -> bool:
    d = (a - b) & SEQ_MASK
    return 0 < d < SEQ_HALF


# --------------------------------------------------------------------------
# Opus wrapper
# --------------------------------------------------------------------------

class OpusCodec:
    """Opus encoder/decoder fixed to spec. Not thread-safe; use from one thread."""

    def __init__(self, bitrate: int = BITRATE):
        # Must precede `import opuslib`: it calls find_library('opus') at import.
        _ensure_opus_dll()
        try:
            import opuslib
        except Exception as ex:
            raise RuntimeError(
                "Could not load the Opus library.\n"
                "Windows: put 64-bit opus.dll (matching your Python) next to "
                "p2p_call.py — see README troubleshooting. "
                "Download: https://github.com/ShiftMediaProject/opus/releases\n"
                "Linux: sudo apt install libopus0 (Debian) / "
                "sudo pacman -S opus (Arch).\n"
                f"Underlying error: {ex}"
            ) from ex
        self._opuslib = opuslib
        try:
            app = opuslib.APPLICATION_RESTRICTED_LOWDELAY
        except AttributeError:
            app = "restricted_lowdelay"
        try:
            self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, app)
        except Exception as ex:
            raise RuntimeError(
                "Failed to create Opus encoder. libopus missing?\n"
                "Linux: sudo apt install libopus0\n"
                "Windows: put opus.dll next to p2p_call.py or on PATH.\n"
                f"Underlying error: {ex}"
            ) from ex
        # Hard CBR per spec.
        self.encoder.bitrate = bitrate
        self.encoder.vbr = 0  # 0 = hard CBR
        try:
            self.encoder.vbr_constraint = 0
        except Exception:
            pass
        self.decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)

    @property
    def actual_bitrate(self) -> int:
        try:
            return int(self.encoder.bitrate)
        except Exception:
            return -1

    @property
    def actual_vbr(self) -> int:
        try:
            return int(self.encoder.vbr)
        except Exception:
            return -1

    def encode_frame(self, pcm16_bytes: bytes) -> bytes:
        return bytes(self.encoder.encode(pcm16_bytes, FRAME_SIZE))

    def decode_frame(self, payload: bytes) -> bytes:
        return bytes(self.decoder.decode(payload, FRAME_SIZE))

    def decode_plc(self) -> bytes:
        """Packet-loss concealment: decode empty payload -> 120 samples."""
        try:
            return bytes(self.decoder.decode(b"", FRAME_SIZE))
        except Exception:
            return b"\x00" * (FRAME_SIZE * 2)


# --------------------------------------------------------------------------
# Jitter buffer
# --------------------------------------------------------------------------

class JitterStats:
    def __init__(self):
        self.received = 0
        self.duplicates = 0
        self.dropped_late = 0      # out-of-order + playback time passed
        self.dropped_overflow = 0
        self.played = 0
        self.skipped_gap = 0       # packets jumped over (lost)
        self.underflows = 0


class JitterBuffer:
    """Depth-limited, in-order playout buffer keyed by uint32 seq.

    Policy (matches spec):
      - push() drops packets older than `expected` (out-of-order and their
        playback time has passed) and counts them as dropped_late.
      - pull() returns payloads strictly in seq order. Missing seqs yield
        None (caller outputs silence/PLC) until the buffer fills up, at
        which point the gap is skipped and counted as lost.
    Thread-safe.
    """

    def __init__(self, maxlen: int = DEFAULT_BUFFER):
        self.maxlen = max(1, int(maxlen))
        self._buf: dict[int, bytes] = {}
        self._lock = threading.Lock()
        self._expected: int | None = None
        self._started = False
        self._pre_roll: int = min(2, max(1, self.maxlen // 2))
        self.stats = JitterStats()
        self._last_recv_time = 0.0

    def set_maxlen(self, n: int) -> None:
        with self._lock:
            self.maxlen = max(1, int(n))
            self._pre_roll = min(2, max(1, self.maxlen // 2))
            # Trim oldest if overfull.
            while len(self._buf) > self.maxlen:
                oldest = min(self._buf, key=lambda s: seq_diff(s, self._expected or s))
                del self._buf[oldest]
                self.stats.dropped_overflow += 1

    def reset(self) -> None:
        with self._lock:
            self._buf.clear()
            self._expected = None
            self._started = False

    def push(self, seq: int, payload: bytes) -> bool:
        seq &= SEQ_MASK
        with self._lock:
            self.stats.received += 1
            self._last_recv_time = time.monotonic()
            if self._expected is None:
                self._expected = seq
                self._started = False
            if seq == self._expected or is_newer(seq, self._expected):
                if seq in self._buf:
                    self.stats.duplicates += 1
                    return False
                # If pre-roll not done and this is the first burst, just store.
                self._buf[seq] = payload
                if len(self._buf) > self.maxlen:
                    exp0 = self._expected if self._expected is not None else seq
                    candidates = sorted(self._buf, key=lambda s: seq_diff(s, exp0))
                    # Keep the newest `maxlen`, drop the rest (oldest).
                    for old in candidates[: len(self._buf) - self.maxlen]:
                        del self._buf[old]
                        self.stats.dropped_overflow += 1
                return True
            else:
                # Older than expected -> playback time has passed -> skip.
                self.stats.dropped_late += 1
                return False

    def pull(self) -> bytes | None:
        """Return next in-order payload, or None if underflow/gap-wait."""
        with self._lock:
            if self._expected is None:
                self.stats.underflows += 1
                return None
            exp = self._expected
            if exp in self._buf:
                if not self._started:
                    if len(self._buf) < self._pre_roll:
                        self.stats.underflows += 1
                        return None
                    self._started = True
                payload = self._buf.pop(exp)
                self._expected = (exp + 1) & SEQ_MASK
                self.stats.played += 1
                # Opportunistic stale purge: drop anything older than new expected
                # (should be rare; protects against wrap edge cases).
                stale = [s for s in self._buf if not (s == self._expected or is_newer(s, self._expected))]
                for s in stale:
                    del self._buf[s]
                    self.stats.dropped_late += 1
                return payload
            # Gap: expected missing.
            if not self._buf:
                self.stats.underflows += 1
                return None
            if len(self._buf) >= self.maxlen:
                # Must advance: skip the gap, jump to oldest buffered newer seq.
                nxt = min(self._buf, key=lambda s: seq_diff(s, exp))
                gap = seq_diff(nxt, exp)
                # gap >= 1 (nxt != exp). Packets between exp..nxt-1 are lost.
                self.stats.skipped_gap += max(0, gap - 1) + 1  # +1 counts the jump itself
                # Note: skipped_gap counts frames skipped; played counts returned.
                payload = self._buf.pop(nxt)
                self._expected = (nxt + 1) & SEQ_MASK
                self._started = True
                self.stats.played += 1
                return payload
            self.stats.underflows += 1
            return None

    def idle_reset_if_stale(self, timeout_s: float = 1.0) -> None:
        """If no packets for a while (peer restarted), reset so next seq re-syncs."""
        with self._lock:
            if self._expected is not None and self._buf == {}:
                if self._last_recv_time and (time.monotonic() - self._last_recv_time) > timeout_s:
                    self._expected = None
                    self._started = False


# --------------------------------------------------------------------------
# P2P call engine
# --------------------------------------------------------------------------

class P2PCall:
    def __init__(self, mode: str, port: int = DEFAULT_PORT,
                 address: str = "127.0.0.1", frame_buffer: int = DEFAULT_BUFFER,
                 password: str = "", volume: float = 1.0,
                 log=None):
        assert mode in ("host", "peer")
        self.mode = mode
        self.port = int(port)
        self.address = address
        self.frame_buffer = max(1, int(frame_buffer))
        self.password = password or ""
        # Receive-volume gain (0.0-2.0). Written by the UI thread, read by the
        # audio thread; plain float assignment is atomic under the GIL.
        self.volume = max(0.0, min(2.0, float(volume)))
        self.log = log or (lambda *a: None)
        self.sock: socket.socket | None = None
        self.peer_addr = None       # Host: learned; Peer: destination (updated on reply)
        self.dest = None            # Peer only
        self.jitter = JitterBuffer(self.frame_buffer)
        self.codec: OpusCodec | None = None
        self._aesgcm = None         # AESGCM instance when password set, else None
        self.stream = None
        self._stop = threading.Event()
        self._recv_thread: threading.Thread | None = None
        self._send_seq = 0
        self.sent = 0
        self.send_errors = 0
        self.decrypt_errors = 0
        # Audio FIFOs (int16 mono samples). Accessed only from audio callback thread.
        self._in_fifo = None   # numpy array
        self._out_fifo = None
        self._np = None
        self._status_msg = ""
        self._lock = threading.Lock()

    # -- socket setup -----------------------------------------------------
    def _setup_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        # No send buffer: flush 1 frame per packet immediately.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        except OSError:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        except OSError:
            pass
        try:
            # Expedited forwarding hint; best-effort, ignore failures.
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
        except OSError:
            pass
        if self.mode == "host":
            s.bind(("0.0.0.0", self.port))
            s.settimeout(0.5)
            self.peer_addr = None
            self.log(f"Host bound 0.0.0.0:{self.port}, waiting for peer...")
        else:
            s.bind(("0.0.0.0", 0))
            s.settimeout(0.5)
            self.dest = (self.address, self.port)
            self.peer_addr = self.dest
            self.log(f"Peer bound ephemeral, destination {self.dest}")
        self.sock = s

    # -- public control ---------------------------------------------------
    def start(self):
        if self.stream is not None or (self._recv_thread and self._recv_thread.is_alive()):
            raise RuntimeError("Already running")
        try:
            import numpy as np
        except ImportError as ex:
            raise RuntimeError("numpy not installed. Run: pip install numpy") from ex
        try:
            import sounddevice as sd
        except ImportError as ex:
            raise RuntimeError(
                "sounddevice not installed. Run: pip install sounddevice\n"
                "Linux may need libportaudio2: sudo apt install libportaudio2"
            ) from ex
        self._np = np
        self._in_fifo = np.zeros(0, dtype=np.int16)
        self._out_fifo = np.zeros(0, dtype=np.int16)
        self.codec = OpusCodec(BITRATE)
        if self.password:
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            except ImportError as ex:
                raise RuntimeError(
                    "Encryption needs the 'cryptography' package. "
                    "Run: pip install cryptography"
                ) from ex
            self._aesgcm = AESGCM(derive_opus_key(self.password))
        else:
            self._aesgcm = None
        self._setup_socket()
        self.jitter.set_maxlen(self.frame_buffer)
        self.jitter.reset()
        self._send_seq = 0
        self.sent = 0
        self.send_errors = 0
        self.decrypt_errors = 0
        self._stop.clear()

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True,
                                             name=f"p2p-recv-{self.mode}")
        self._recv_thread.start()

        try:
            self.stream = sd.Stream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAME_SIZE,   # request 2.5 ms; FIFO handles any actual size
                channels=CHANNELS,
                dtype="int16",
                latency="low",
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception:
            self._stop.set()
            try:
                if self.sock:
                    self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.stream = None
            raise
        self.log(
            f"Started {self.mode} opus={self.codec.actual_bitrate}bps "
            f"vbr={self.codec.actual_vbr} fsize={FRAME_SIZE} fb={self.frame_buffer} "
            f"enc={'on' if self._aesgcm is not None else 'off'} "
            f"vol={int(round(self.volume * 100))}%"
        )

    def stop(self):
        self._stop.set()
        st = self.stream
        self.stream = None
        if st is not None:
            try:
                st.stop()
            except Exception:
                pass
            try:
                st.close()
            except Exception:
                pass
        rt = self._recv_thread
        if rt is not None:
            rt.join(timeout=1.5)
        self._recv_thread = None
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.log("Stopped.")

    @property
    def running(self) -> bool:
        return self.stream is not None

    # -- network recv -----------------------------------------------------
    def _recv_loop(self):
        assert self.sock is not None
        aes = self._aesgcm  # fixed for the life of the call
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(
                    MAX_OPUS_BYTES + SEQ_STRUCT.size + GCM_TAG_LEN)
            except socket.timeout:
                self.jitter.idle_reset_if_stale()
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.01)
                continue
            if len(data) < SEQ_STRUCT.size + 1:
                continue
            seq = SEQ_STRUCT.unpack_from(data, 0)[0]
            payload = bytes(data[SEQ_STRUCT.size:])
            if aes is not None:
                # Encrypted: payload = ciphertext + 16-byte GCM tag.
                if len(payload) < 1 + GCM_TAG_LEN:
                    continue
                try:
                    payload = bytes(aes.decrypt(
                        gcm_nonce(seq), payload, SEQ_STRUCT.pack(seq)))
                except Exception:
                    # Wrong password, tampered or spliced packet: drop it.
                    self.decrypt_errors += 1
                    continue
            # Learn / track peer.
            if self.mode == "host":
                if self.peer_addr != addr:
                    self.peer_addr = addr
                    self.log(f"Host: peer -> {addr}")
            else:
                if self.peer_addr != addr:
                    # Server replies from its bound port; adopt actual source
                    # (handles NAT / reply-port differences).
                    self.peer_addr = addr
            self.jitter.push(seq, payload)

    # -- audio callback (PortAudio thread; must be fast, no blocking) -----
    def _audio_callback(self, indata, outdata, frames, time_info, status):
        np_mod = self._np
        codec = self.codec
        in_fifo = self._in_fifo
        out_fifo = self._out_fifo
        if np_mod is None or codec is None or in_fifo is None or out_fifo is None:
            try:
                outdata.fill(0)
            except Exception:
                pass
            return
        try:
            # --- capture -> encode -> send (1 frame per UDP packet) ---
            try:
                mono_in = np_mod.ascontiguousarray(indata[:, 0], dtype=np_mod.int16)
            except Exception:
                mono_in = np_mod.zeros(frames, dtype=np_mod.int16)
            in_fifo = np_mod.concatenate([in_fifo, mono_in]) if len(in_fifo) else mono_in.copy()
            dest = self.peer_addr  # snapshot; may be None on host before first pkt
            sock = self.sock
            aes = self._aesgcm  # fixed for the life of the call
            while len(in_fifo) >= FRAME_SIZE:
                frame = in_fifo[:FRAME_SIZE]
                in_fifo = in_fifo[FRAME_SIZE:]
                if dest is None or sock is None:
                    continue  # host with no peer yet: drop capture, keep playing
                try:
                    opus = codec.encode_frame(frame.tobytes())
                except Exception:
                    continue
                seq = self._send_seq & SEQ_MASK
                try:
                    if aes is None:
                        pkt = SEQ_STRUCT.pack(seq) + opus
                    else:
                        # AES-256-GCM the opus frame; tag appended to ciphertext.
                        enc = aes.encrypt(gcm_nonce(seq), opus, SEQ_STRUCT.pack(seq))
                        pkt = SEQ_STRUCT.pack(seq) + enc
                except Exception:
                    continue
                try:
                    sock.sendto(pkt, dest)
                    self.sent += 1
                except (BlockingIOError, OSError):
                    self.send_errors += 1
                except Exception:
                    self.send_errors += 1
                self._send_seq = (self._send_seq + 1) & SEQ_MASK
            self._in_fifo = in_fifo

            # --- jitter -> decode -> play (shared receive-volume gain) ---
            need = int(frames)
            gain = self.volume
            apply_gain = gain != 1.0
            while len(out_fifo) < need:
                payload = self.jitter.pull()
                if payload is None:
                    chunk = np_mod.zeros(FRAME_SIZE, dtype=np_mod.int16)
                else:
                    try:
                        raw = codec.decode_frame(payload)
                        chunk = np_mod.frombuffer(raw, dtype=np_mod.int16).copy()
                        if len(chunk) < FRAME_SIZE:
                            pad = np_mod.zeros(FRAME_SIZE - len(chunk), dtype=np_mod.int16)
                            chunk = np_mod.concatenate([chunk, pad])
                        elif len(chunk) > FRAME_SIZE:
                            chunk = chunk[:FRAME_SIZE]
                    except Exception:
                        try:
                            raw = codec.decode_plc()
                            chunk = np_mod.frombuffer(raw, dtype=np_mod.int16).copy()
                        except Exception:
                            chunk = np_mod.zeros(FRAME_SIZE, dtype=np_mod.int16)
                if apply_gain and len(chunk):
                    chunk = np_mod.clip(
                        chunk.astype(np_mod.float32) * gain,
                        -32768, 32767).astype(np_mod.int16)
                out_fifo = np_mod.concatenate([out_fifo, chunk]) if len(out_fifo) else chunk
            outdata[:, 0] = out_fifo[:need]
            if outdata.shape[1] > 1:
                # Duplicate mono if device opened stereo unexpectedly.
                for ch in range(1, outdata.shape[1]):
                    outdata[:, ch] = outdata[:, 0]
            self._out_fifo = out_fifo[need:]
        except Exception:
            # Never raise from audio thread; output silence.
            try:
                outdata.fill(0)
            except Exception:
                pass

    def get_stats(self) -> dict:
        js = self.jitter.stats
        return {
            "mode": self.mode,
            "sent": self.sent,
            "send_errors": self.send_errors,
            "received": js.received,
            "played": js.played,
            "dropped_late": js.dropped_late,
            "dropped_overflow": js.dropped_overflow,
            "skipped_gap": js.skipped_gap,
            "underflows": js.underflows,
            "decrypt_errors": self.decrypt_errors,
            "peer": str(self.peer_addr),
        }


# --------------------------------------------------------------------------
# Address parsing for Peer UI
# --------------------------------------------------------------------------

def parse_peer_address(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Accept 'host', 'host:port', '[v6]:port'. Returns (host, port)."""
    t = (text or "").strip()
    if not t:
        raise ValueError("Address is empty")
    if t.startswith("["):
        # [::1]:8192 or [::1]
        end = t.find("]")
        if end == -1:
            raise ValueError("Bad IPv6 address (missing ])")
        host = t[1:end]
        rest = t[end + 1:].strip()
        if rest.startswith(":"):
            return host, int(rest[1:] or default_port)
        return host, default_port
    if t.count(":") == 1:
        host, p = t.rsplit(":", 1)
        host = host.strip()
        p = p.strip()
        if not host:
            raise ValueError("Bad address")
        return host, int(p) if p else default_port
    # Bare hostname/IPv4/IPv6 without port.
    # Strip any whitespace-zone suffix for link-local (fe80::1%eth0) — keep as-is.
    return t, default_port


# --------------------------------------------------------------------------
# Tkinter UI
# --------------------------------------------------------------------------

def run_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as ex:
        print(f"tkinter unavailable ({ex}). Use CLI mode: python p2p_call.py --help",
              file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("P2P Call — Opus LOWDELAY 2.5 ms")
    root.resizable(False, False)

    calls: dict[str, P2PCall | None] = {"active": None}
    log_q: queue.Queue[str] = queue.Queue()

    def log(msg: str):
        log_q.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def stop_active():
        c = calls["active"]
        calls["active"] = None
        if c is not None:
            try:
                c.stop()
            except Exception as ex:
                log(f"stop error: {ex}")
        host_btn.config(text="Start", state="normal")
        peer_btn.config(text="Start", state="normal")
        host_status.config(text="Stopped")
        peer_status.config(text="Stopped")

    # ---- layout ----
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=8)
    host_frame = ttk.Frame(nb, padding=10)
    peer_frame = ttk.Frame(nb, padding=10)
    nb.add(host_frame, text="Host")
    nb.add(peer_frame, text="Peer")

    # Host tab (connection identity only; buffer/password/volume are shared)
    ttk.Label(host_frame, text="Port:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
    host_port = tk.StringVar(value=str(DEFAULT_PORT))
    ttk.Entry(host_frame, textvariable=host_port, width=10).grid(row=0, column=1, sticky="w", padx=4, pady=4)
    host_btn = ttk.Button(host_frame, text="Start")
    host_btn.grid(row=1, column=0, columnspan=2, pady=8, sticky="ew")
    host_status = ttk.Label(host_frame, text="Stopped")
    host_status.grid(row=2, column=0, columnspan=2, sticky="w")
    host_stats = ttk.Label(host_frame, text="", font=("TkDefaultFont", 8))
    host_stats.grid(row=3, column=0, columnspan=2, sticky="w")

    # Peer tab
    ttk.Label(peer_frame, text="Address:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
    peer_addr = tk.StringVar(value="127.0.0.1")
    ttk.Entry(peer_frame, textvariable=peer_addr, width=22).grid(row=0, column=1, sticky="w", padx=4, pady=4)
    ttk.Label(peer_frame, text="Port:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
    peer_port = tk.StringVar(value=str(DEFAULT_PORT))
    ttk.Entry(peer_frame, textvariable=peer_port, width=10).grid(row=1, column=1, sticky="w", padx=4, pady=4)
    ttk.Label(peer_frame, text="Tip: address may be 'host' or 'host:port'.",
              font=("TkDefaultFont", 8)).grid(row=2, column=0, columnspan=2, sticky="w", padx=4)
    peer_btn = ttk.Button(peer_frame, text="Start")
    peer_btn.grid(row=3, column=0, columnspan=2, pady=8, sticky="ew")
    peer_status = ttk.Label(peer_frame, text="Stopped")
    peer_status.grid(row=4, column=0, columnspan=2, sticky="w")
    peer_stats = ttk.Label(peer_frame, text="", font=("TkDefaultFont", 8))
    peer_stats.grid(row=5, column=0, columnspan=2, sticky="w")

    # ---- shared settings: receive volume, frame buffer, password ----
    # Changing password/buffer takes effect on Stop + Start; volume is live.
    shared_frame = ttk.Frame(root, padding=(10, 0))
    shared_frame.pack(fill="x", padx=0, pady=(0, 8))
    ttk.Label(shared_frame, text="Volume:").grid(row=0, column=0, sticky="e", padx=4)
    shared_vol = tk.IntVar(value=DEFAULT_VOLUME)
    vol_scale = ttk.Scale(shared_frame, from_=0, to=200, variable=shared_vol,
                          orient="horizontal", length=130)
    vol_scale.grid(row=0, column=1, sticky="w", padx=4)
    vol_val = ttk.Label(shared_frame, text=f"{DEFAULT_VOLUME}%", width=5)
    vol_val.grid(row=0, column=2, sticky="w")
    ttk.Label(shared_frame, text="Buffer:").grid(row=0, column=3, sticky="e", padx=4)
    shared_fb = tk.StringVar(value=str(DEFAULT_BUFFER))
    ttk.Spinbox(shared_frame, from_=1, to=64, textvariable=shared_fb, width=5).grid(
        row=0, column=4, sticky="w", padx=4)
    ttk.Label(shared_frame, text="Password:").grid(row=0, column=5, sticky="e", padx=4)
    shared_pw = tk.StringVar(value="")
    ttk.Entry(shared_frame, textvariable=shared_pw, show="*", width=14).grid(
        row=0, column=6, sticky="w", padx=4)

    def _apply_volume(*_args):
        try:
            v = int(shared_vol.get())
        except (ValueError, tk.TclError):
            return
        v = max(0, min(200, v))
        vol_val.config(text=f"{v}%")
        c = calls["active"]
        if c is not None and c.running:
            c.volume = v / 100.0

    shared_vol.trace_add("write", _apply_volume)

    log_box = tk.Text(root, height=6, width=60, state="disabled", font=("TkDefaultFont", 8))
    log_box.pack(fill="both", padx=8, pady=(0, 8))

    def append_log():
        try:
            while True:
                msg = log_q.get_nowait()
                log_box.config(state="normal")
                log_box.insert("end", msg + "\n")
                log_box.see("end")
                log_box.config(state="disabled")
        except queue.Empty:
            pass
        # stats refresh
        c = calls["active"]
        if c is not None:
            try:
                s = c.get_stats()
                txt = (f"peer={s['peer']} sent={s['sent']} recv={s['received']} "
                       f"played={s['played']} late={s['dropped_late']} "
                       f"ovf={s['dropped_overflow']} skip={s['skipped_gap']} "
                       f"und={s['underflows']} dec={s['decrypt_errors']}")
                if c.mode == "host":
                    host_stats.config(text=txt)
                else:
                    peer_stats.config(text=txt)
            except Exception:
                pass
        root.after(250, append_log)

    def on_host_toggle():
        c = calls["active"]
        if c is not None and c.mode == "host" and c.running:
            stop_active()
            return
        if c is not None:
            stop_active()
        try:
            p = int(host_port.get().strip())
            fb = int(shared_fb.get().strip())
            vol = int(shared_vol.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Host", "Port, buffer and volume must be integers")
            return
        if not (1 <= p <= 65535 and 1 <= fb <= 64 and 0 <= vol <= 200):
            messagebox.showerror("Host", "Port 1-65535, buffer 1-64, volume 0-200")
            return
        call = P2PCall("host", port=p, frame_buffer=fb,
                       password=shared_pw.get(), volume=vol / 100.0, log=log)
        try:
            call.start()
        except Exception as ex:
            messagebox.showerror("Host start failed", str(ex))
            log(f"Host start failed: {ex}")
            try:
                call.stop()
            except Exception:
                pass
            return
        calls["active"] = call
        host_btn.config(text="Stop")
        peer_btn.config(text="Start", state="disabled")
        host_status.config(text=f"Hosting on :{p} (fb={fb}) — waiting for peer…")
        if not _save_ui_snapshot("host"):
            log("Could not save p2p_call.ini")

    def on_peer_toggle():
        c = calls["active"]
        if c is not None and c.mode == "peer" and c.running:
            stop_active()
            return
        if c is not None:
            stop_active()
        addr_text = peer_addr.get()
        try:
            if ":" in addr_text and peer_port.get().strip() not in ("", str(DEFAULT_PORT)):
                # If user typed host:port in address AND set port field, address wins if it has explicit port.
                try:
                    host, pp = parse_peer_address(addr_text, int(peer_port.get().strip() or DEFAULT_PORT))
                except ValueError:
                    host, pp = addr_text.strip(), int(peer_port.get().strip())
            else:
                # Combine fields: address field may already contain :port.
                try:
                    host, pp = parse_peer_address(addr_text, int(peer_port.get().strip() or DEFAULT_PORT))
                except ValueError as ex:
                    messagebox.showerror("Peer", str(ex))
                    return
            fb = int(shared_fb.get().strip())
            vol = int(shared_vol.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Peer", "Port / buffer / volume must be integers")
            return
        if not (1 <= pp <= 65535 and 1 <= fb <= 64 and 0 <= vol <= 200):
            messagebox.showerror("Peer", "Port 1-65535, buffer 1-64, volume 0-200")
            return
        if not host:
            messagebox.showerror("Peer", "Address is empty")
            return
        call = P2PCall("peer", port=pp, address=host, frame_buffer=fb,
                       password=shared_pw.get(), volume=vol / 100.0, log=log)
        try:
            call.start()
        except Exception as ex:
            messagebox.showerror("Peer start failed", str(ex))
            log(f"Peer start failed: {ex}")
            try:
                call.stop()
            except Exception:
                pass
            return
        calls["active"] = call
        peer_btn.config(text="Stop")
        host_btn.config(text="Start", state="disabled")
        peer_status.config(text=f"Calling {host}:{pp} (fb={fb})…")
        if not _save_ui_snapshot("peer"):
            log("Could not save p2p_call.ini")

    host_btn.config(command=on_host_toggle)
    peer_btn.config(command=on_peer_toggle)

    def _save_ui_snapshot(mode: str) -> bool:
        """Persist the whole-UI snapshot. Returns False on invalid/failed write."""
        try:
            return save_config(
                mode,
                int(host_port.get().strip()),
                peer_addr.get().strip(),
                int(peer_port.get().strip()),
                int(shared_fb.get().strip()),
                int(shared_vol.get()),
                shared_pw.get(),
            )
        except (ValueError, AttributeError, tk.TclError):
            return False

    # ---- restore last session (whole-UI snapshot) ----
    try:
        _cfg = load_config()
        if "host_port" in _cfg:
            host_port.set(str(_cfg["host_port"]))
        if "peer_address" in _cfg:
            peer_addr.set(_cfg["peer_address"])
        if "peer_port" in _cfg:
            peer_port.set(str(_cfg["peer_port"]))
        if "buffer" in _cfg:
            shared_fb.set(str(_cfg["buffer"]))
        if "volume" in _cfg:
            shared_vol.set(_cfg["volume"])
            vol_val.config(text=f"{_cfg['volume']}%")
        if "password" in _cfg:
            shared_pw.set(_cfg["password"])
        if _cfg.get("mode") == "peer":
            nb.select(peer_frame)
        elif _cfg.get("mode") == "host":
            nb.select(host_frame)
    except Exception as ex:
        log(f"Config restore failed: {ex}")

    def on_close():
        # Persist the whole-UI snapshot with the visible tab as mode.
        try:
            _save_ui_snapshot("host" if nb.index(nb.select()) == 0 else "peer")
        except (ValueError, AttributeError, tk.TclError):
            pass
        try:
            stop_active()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log("Ready. Bitrate=98304 hard-CBR, 48kHz mono, 2.5ms LOWDELAY.")
    root.after(250, append_log)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# Headless CLI (fallback when tkinter missing + useful for tests)
# --------------------------------------------------------------------------

def run_check_opus() -> int:
    """Diagnostic dump for opus.dll problems (mainly Windows). Debug-only."""
    print(f"platform={sys.platform} python={sys.version.split()[0]} "
          f"bitness={struct.calcsize('P') * 8}-bit exe={sys.executable}")
    dirs = []
    try:
        dirs.append(("script-dir", str(Path(__file__).resolve().parent)))
    except Exception as ex:
        print(f"script-dir: <unresolvable: {ex}>")
    try:
        dirs.append(("cwd", os.getcwd()))
    except Exception as ex:
        print(f"cwd: <unresolvable: {ex}>")
    try:
        dirs.append(("exe-dir", str(Path(sys.executable).resolve().parent)))
    except Exception as ex:
        print(f"exe-dir: <unresolvable: {ex}>")
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        dirs.append(("bundle", str(bundle)))
    for label, d in dirs:
        print(f"[{label}] {d}")
        if not os.path.isdir(d):
            print("  (not a directory)")
            continue
        try:
            dlls = sorted(f for f in os.listdir(d) if f.lower().endswith(".dll"))
        except Exception as ex:
            print(f"  (cannot list: {ex})")
            continue
        if not dlls:
            print("  (no .dll files)")
            continue
        for f in dlls:
            full = os.path.join(d, f)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            try:
                ctypes.CDLL(full)
                print(f"  {f} ({size} bytes): LOADS OK")
            except Exception as ex:
                # The detail matters: WinError 126 = missing *dependency* of
                # this DLL (MSVC runtime / MinGW siblings), 193 = bitness
                # mismatch (32-bit DLL vs 64-bit Python or vice versa).
                print(f"  {f} ({size} bytes): LOAD FAILED: {type(ex).__name__}: {ex}")
    print(f"find_library('opus') -> {ctypes.util.find_library('opus')}")
    # Bare-name probe FIRST (Windows only): this is exactly what opuslib does
    # (CDLL(find_library(...))). A bare filename loads via Windows DLL search,
    # which excludes the script dir / CWD — unlike the full-path loads below,
    # which always work and would otherwise mask this failure.
    if sys.platform == "win32":
        try:
            ctypes.CDLL("opus.dll")
            print("bare-name CDLL('opus.dll') -> LOADS OK")
        except Exception as ex:
            print(f"bare-name CDLL('opus.dll') -> FAILED: {type(ex).__name__}: {ex}")
    print(f"_ensure_opus_dll() -> {_ensure_opus_dll()}")
    print(f"patched find_library('opus') -> {ctypes.util.find_library('opus')}")
    try:
        import tkinter
        print(f"tkinter OK (Tk {tkinter.TkVersion})")
    except Exception as ex:
        print(f"tkinter MISSING: {ex}")
    try:
        import sounddevice as sd
        print(f"sounddevice OK, PortAudio {sd.get_portaudio_version()[1]}")
    except Exception as ex:
        print(f"sounddevice FAILED: {ex}")
    try:
        c = OpusCodec()
        print(f"OpusCodec OK: bitrate={c.actual_bitrate} vbr={c.actual_vbr}")
        return 0
    except Exception as ex:
        print(f"OpusCodec FAILED:\n{ex}")
        return 1


def run_cli(args) -> int:
    if getattr(args, "check_opus", False):
        return run_check_opus()
    if args.list_devices:
        try:
            import sounddevice as sd
        except ImportError:
            print("sounddevice not installed", file=sys.stderr)
            return 2
        print(sd.query_devices())
        return 0
    # Config supplies defaults; explicit CLI flags win. Mode itself defaults
    # to the saved mode so `python p2p_call.py --cli` resumes the last session.
    need_cfg = (args.mode is None or args.port is None or args.buffer is None
                or args.address is None or args.volume is None or args.password is None)
    cfg = load_config() if need_cfg else {}
    mode = args.mode or cfg.get("mode")
    buffer = args.buffer if args.buffer is not None else cfg.get("buffer", DEFAULT_BUFFER)
    volume = args.volume if args.volume is not None else cfg.get("volume", DEFAULT_VOLUME)
    password = args.password if args.password is not None else cfg.get("password", "")
    if args.mode == "host" or (args.mode is None and mode == "host"):
        port = (args.port if args.port is not None
                else cfg.get("host_port", DEFAULT_PORT))
        call = P2PCall("host", port=port, frame_buffer=buffer,
                       password=password, volume=volume / 100.0,
                       log=lambda m: print(m, flush=True))
    elif args.mode == "peer" or (args.mode is None and mode == "peer"):
        address = args.address or cfg.get("peer_address")
        if not address:
            print("--address required for peer mode", file=sys.stderr)
            return 2
        default_port = (args.port if args.port is not None
                        else cfg.get("peer_port", DEFAULT_PORT))
        host, pp = parse_peer_address(address, default_port)
        call = P2PCall("peer", port=pp, address=host, frame_buffer=buffer,
                       password=password, volume=volume / 100.0,
                       log=lambda m: print(m, flush=True))
    else:
        print("No --mode given; launching GUI...", flush=True)
        return run_gui()
    print(f"Starting {call.mode} (bitrate={BITRATE} CBR, fsize={FRAME_SIZE})...", flush=True)
    try:
        call.start()
    except Exception as ex:
        print(f"Start failed: {ex}", file=sys.stderr)
        return 1
    print("Running. Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(0.5)
            s = call.get_stats()
            print(f"\rpeer={s['peer']} sent={s['sent']} recv={s['received']} "
                  f"played={s['played']} late={s['dropped_late']} skip={s['skipped_gap']} "
                  f"und={s['underflows']} dec={s['decrypt_errors']}", end="", flush=True)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        call.stop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P2P Opus LOWDELAY voice call")
    ap.add_argument("--mode", choices=["host", "peer"], default=None)
    ap.add_argument("--port", type=int, default=None,
                    help=f"UDP port (default: saved value or {DEFAULT_PORT})")
    ap.add_argument("--address", default=None, help="Peer: host or host:port")
    ap.add_argument("--buffer", type=int, default=None,
                    help=f"Frame buffer in frames (default: saved value or {DEFAULT_BUFFER})")
    ap.add_argument("--volume", type=int, default=None,
                    help=f"Receive volume 0-200%% (default: saved value or {DEFAULT_VOLUME})")
    ap.add_argument("--password", default=None,
                    help="Shared password for AES-256-GCM (default: saved value or none)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--check-opus", action="store_true",
                    help="Diagnose opus.dll loading (Windows troubleshooting)")
    ap.add_argument("--cli", action="store_true", help="Force CLI even if GUI available")
    ns = ap.parse_args(argv)
    if ns.cli or ns.mode is not None or ns.list_devices or ns.check_opus:
        return run_cli(ns)
    # Default: GUI.
    try:
        return run_gui()
    except Exception as ex:
        print(f"GUI failed ({ex}), falling back to CLI. See --help.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
