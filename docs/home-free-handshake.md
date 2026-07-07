# BattleLab Home Free — E2E handshake wire spec (v1)

The relay is **blind**: it forwards the binary frames below verbatim and can read
none of them. This document pins the wire format precisely so the Python agent
and the browser (WebCrypto) implementations interoperate byte-for-byte.

All primitives are WebCrypto-available: **X25519** (`deriveBits`), **HKDF-SHA256**,
**AES-256-GCM**, SHA-256.

## Roles

- **Initiator** = the browser viewer.
- **Responder** = the agent on the user's box.

Both hold the **access key** (the installer-generated secret). The relay never
sees it.

## Key schedule

```
PSK  = HKDF-SHA256(ikm = utf8(access_key), salt = "", info = "battlelab-home-free psk v1", len = 32)
```

Each side generates an **ephemeral X25519** keypair (forward secrecy):

```
DH   = X25519(own_ephemeral_priv, peer_ephemeral_pub)          # 32 bytes
TH   = SHA-256( "battlelab-home-free/v1" || e_i || e_r )       # transcript hash, 32 bytes
OKM  = HKDF-SHA256(ikm = DH, salt = PSK, info = "bl-hf keys v1" || TH, len = 64)
k_i2r = OKM[0:32]        # initiator -> responder
k_r2i = OKM[32:64]       # responder -> initiator
```

Mixing the **PSK as the HKDF salt** means a man-in-the-middle without the access
key derives different keys and cannot produce a valid confirmation — this is what
authenticates both parties (an NNpsk0-style pattern).

`e_i` / `e_r` are the 32-byte raw X25519 public keys of initiator / responder.

## Messages (each a single binary relay frame)

```
msg1  initiator -> responder :  e_i                              (32 bytes)
msg2  responder -> initiator :  e_r || conf_r                    (32 + 16 = 48 bytes)
msg3  initiator -> responder :  conf_i                           (16 bytes)
```

Confirmations are AES-256-GCM tags over an **empty plaintext** with the transcript
hash as AAD and the all-zero nonce (nonce/counter 0 is reserved for confirmation
and never reused for transport):

```
conf_r = AES256GCM(k_r2i).seal(nonce = 0^12, plaintext = "", aad = TH)   # 16-byte tag
conf_i = AES256GCM(k_i2r).seal(nonce = 0^12, plaintext = "", aad = TH)   # 16-byte tag
```

- The responder sends `conf_r`; the initiator MUST verify it (open with `k_r2i`,
  nonce 0, aad TH) before sending data. Failure ⇒ PSK mismatch / MITM ⇒ abort.
- The initiator sends `conf_i`; the responder MUST verify it before sending data.

## Transport records

After the handshake, each direction is an AES-256-GCM stream with its own key and
a **monotonic 64-bit counter starting at 1** (0 was the confirmation). The counter
is sent explicitly so out-of-order / replayed frames are rejected:

```
nonce   = 0x00000000 || counter_u64_be                          # 12 bytes
frame   = counter_u64_be || AES256GCM(key).seal(nonce, plaintext, aad = "")
```

Receiver: parse `counter`; require `counter > last_seen_counter` (strictly
increasing); build the nonce; open; on success update `last_seen_counter`. The
relay delivers frames in order per leg (single WebSocket), so the counter only
ever advances under honest conditions; a gap or repeat is treated as an attack and
the frame is dropped.

## Properties

- **Forward secrecy** — ephemeral X25519 both sides; the access key alone never
  decrypts a captured session.
- **Mutual authentication** — the PSK gates the key schedule; neither confirmation
  verifies without it.
- **Blind relay** — every byte above is opaque to the relay; a plaintext search of
  frames at the relay finds ciphertext only (enforced by a regression test).
- **No nonce reuse** — confirmation uses counter 0; transport starts at 1 and never
  repeats per key.
