# HQ contact portraits: sources, provenance and licences

Provenance record for the 13 HQ contact loops in this directory (issue #1525). Each contact `<id>` ships as `<id>.webm` (VP9), `<id>.mp4` (H.264) and `<id>.jpg` (poster = first frame of the loop), 240×320, no audio.

> **Licence status: read [Licences](#licences) before shipping.** The stills come from Ideogram 4, whose weights are under the *Ideogram Non-Commercial Model Agreement*. That agreement expressly excludes generating output "to include in, or to advertise or promote, revenue-generating products or services" from its permitted use. Every asset here, video and poster, is built on one of those stills.

## How they were made

All generation ran on 2026-10-20 on a local workstation (RTX 5090 + 3090) through a self-hosted media dashboard that wraps the two models below. *Run id* in this file is that dashboard's run identifier; each run record keeps the exact caption or prompt, the seed and the parameters, and the values below are copied from those records.

1. **Still** (768×1344): text-to-image with **Ideogram 4, nf4 quantisation** (`ideogram-ai/ideogram-4-nf4-diffusers`), driven by a structured JSON caption ([Still captions](#still-captions)). Request body: `{"model": "ideogram-image", "caption": <caption>, "size": "768x1344", "n": 1, "response_format": "b64_json", "seed": <seed>}`; the dashboard records the run as `preset: default`.
2. **Clip** (704×1280, 121 frames, 24 fps, ≈5 s): image-to-video with **Wan 2.2 TI2V-5B** (`Wan-AI/Wan2.2-TI2V-5B-Diffusers`, revision `b8fff73`), conditioned on the still (the dashboard scales it to 1280 px high and centre-crops it to 704×1280). Shared settings for all 13: mode `i2v`, 121 frames, 24 fps, **50 steps**, CFG 5.0, **seed 4242**, bf16, VAE tiling on (tile 512). Prompts: [Wan prompts](#wan-prompts).
3. **Loop**: ffmpeg crops a 3:4 head-to-mid-chest window, slows the clip to 0.8× with motion-compensated interpolation, ping-pongs it into a seamless 296-frame (12.33 s) loop and encodes WebM + MP4 + JPEG ([Loop and encode](#loop-and-encode)).

## Per-asset record

Still model for every row: Ideogram 4 nf4. Loop model for every row: Wan 2.2 TI2V-5B, seed 4242, 50 steps. Where two still run ids are listed, the same request was submitted twice and both runs returned byte-identical images.

| id | still seed | still run id | still caption | Wan run id | Wan prompt | crop `y` (px) |
|---|---|---|---|---|---|---|
| `asia-woman` | 8101 | `20261020-094418-api` | template, row below | `20261020-103500-hqc-asia-woman-8479` | variant A, woman | 105 |
| `asia-man` | 8102 | `20261020-094526-api` | template, row below | `20261020-105522-hqc-asia-man-af0c` | variant A, man | 100 |
| `europe-woman` | 8103 | `20261020-094623-api` | template, row below | `20261020-111559-hqc-europe-woman-3df3` | variant A, woman | 124 |
| `europe-man` | 8104 | `20261020-094728-api` | template, row below | `20261020-113626-hqc-europe-man-cd24` | variant A, man | 100 |
| `russia-woman` | 8105 | `20261020-094847-api` | template, row below | `20261020-115655-hqc-russia-woman-566a` | variant B, woman | 105 |
| `russia-man` | 8106 | `20261020-094950-api` | template, row below | `20261020-121659-hqc-russia-man-68d2` | variant B, man | 105 |
| `us-woman` | 8107 | `20261020-095055-api` | template, row below | `20261020-123751-hqc-us-woman-4c85` | variant B, woman | 114 |
| `us-man` | 8108 | `20261020-095213-api` | template, row below | `20261020-125812-hqc-us-man-472c` | variant C, man | 106 |
| `africa-woman` | 8109 | `20261020-095331-api` | template, row below | `20261020-131833-hqc-africa-woman-34d5` | variant C, woman | 98 |
| `africa-man` | 8110 | `20261020-095450-api` / `20261020-095544-api` | template, row below | `20261020-133845-hqc-africa-man-4b17` | variant C, man | 103 |
| `southamerica-woman` | 8111 | `20261020-095639-api` / `20261020-095731-api` | template, row below | `20261020-135841-hqc-southamerica-woman-9d6b` | variant C, woman | 109 |
| `southamerica-man` | 8112 | `20261020-095824-api` / `20261020-095917-api` | template, row below | `20261020-141903-hqc-southamerica-man-4b59` | variant C, man | 103 |
| `romania-woman` | 7604 | `20261020-093646-api` | [verbatim](#romania-woman-caption) | `20261020-101431-hqc-outlaw-woman-a479` | [verbatim](#romania-woman-prompt) | 114 |

`romania-woman` (General Ileana Lupu) was produced under the working name `outlaw-woman`: its Wan run is named `hqc-outlaw-woman` and the files were renamed on delivery, byte for byte. Its still is a separate design with its own caption and seed, not the regional template.

Chain check (2026-10-20): each still was matched to its run by image hash. The image each Wan run was conditioned on matches that still after the dashboard's resize and crop. Each Wan run's `video.mp4` is byte-identical to the clip that was looped. Re-running the loop pipeline below on those clips reproduces the shipped MP4 and JPEG files byte for byte, and the WebM files packet for packet (only the muxer header differs).

## Still captions

### Regional template (12 assets)

The 12 regional captions are one template. Substitute the `{…}` placeholders with the constants and the per-asset row below, exactly as written, and the result is byte-identical to the caption sent for that asset. `{CREST}` and `{UNIFORM}` themselves contain `{TRIM}` / `{UNIFORM_CUT}`, so substitute those first.

```json
{
 "aspect_ratio": "9:16",
 "high_level_description": "{FRAMING}. A portrait of {FACE}, {MOOD}, wearing {CAP}, with {CREST} as the cap badge and {UNIFORM}. Background: {BG}. Lighting: {LIGHT}. {STYLE}. {NOTEXT}",
 "compositional_deconstruction": {
  "background": "{BG}, {LIGHT}",
  "elements": [
   {
    "type": "obj",
    "bbox": [
     120,
     230,
     520,
     770
    ],
    "desc": "{FACE}, wearing {CAP}, with {CREST} as the cap badge; {MOOD}; face and eyes in sharp focus, an original face that resembles no real person"
   },
   {
    "type": "obj",
    "bbox": [
     480,
     80,
     1000,
     920
    ],
    "desc": "upper body to mid-chest in {UNIFORM}"
   },
   {
    "type": "obj",
    "bbox": [
     0,
     0,
     1000,
     1000
    ],
    "desc": "{BG}, {LIGHT}, {NOTEXT}"
   }
  ]
 }
}
```

Constants (identical for all 12):

```
FRAMING:
upper-body portrait framed from the top of the head to mid-chest, centred, eye-level camera, 85mm lens, the whole cap fully in frame with clear headroom above it, both shoulders and epaulettes in frame, not a close-up

MOOD:
sexy and dedicated: strikingly attractive and magnetic, a smouldering, alluring, confident gaze with slightly heavy-lidded eyes straight into the camera, lips closed or slightly parted, no smile, no grin; a disciplined, mission-focused commanding officer you would follow, upright posture, immaculate uniform; classy and adult, fully covered, nothing revealing; polished strategy video game commander portrait, mission briefing energy, heightened saturated colour

BG:
the same dim underground HQ command bunker: a large glowing tactical map screen directly behind, softly out of focus, showing abstract teal terrain contour lines and small amber marker dots with no characters, a subtle teal holographic HUD haze of thin abstract arcs and grid lines floating in the air, dark steel walls falling off to near-black at the edges

LIGHT:
warm golden key light from the front-left, a strong glowing amber rim light from behind on the left outlining the head, cap and shoulder, a strong glowing teal rim light from behind on the right outlining the other side, deep soft shadows, cinematic teal-and-amber colour grade, rich contrast, heightened saturated game colour

STYLE:
high-end cinematic strategy game commander portrait, photoreal with a polished, glossy game-cinematic finish, sharp focus on the eyes, detailed natural skin texture, shallow depth of field

CREST:
a small fictional HQ crest of an amber-gold upward chevron beneath a small four-pointed star, with {TRIM} trim

UNIFORM:
a fitted {UNIFORM_CUT}, with {TRIM} piping, dark epaulettes edged in {TRIM}, two small plain fictional ribbon bars on the left chest with no symbols, and the HQ crest as a small pin on the collar

NOTEXT:
no text, no letters, no numbers, no logos, no flags, no national emblems, no real rank insignia, no five-pointed stars, no political symbols
```

Per-asset values:

| id | `{FACE}` | `{CAP}` | `{UNIFORM_CUT}` | `{TRIM}` |
|---|---|---|---|---|
| `asia-woman` | an original fictional East Asian woman aged about 32, clearly adult, beautiful, smooth porcelain skin, dark almond-shaped eyes, defined brows, sleek black hair pulled back into a neat low chignon | a dark charcoal peaked officer's cap with a black visor | dark charcoal high mandarin-collared tunic | jade-green |
| `asia-man` | an original fictional East Asian man aged about 40, clearly adult, handsome, clean-shaven, strong cheekbones, dark eyes, short neat black hair | a dark charcoal peaked officer's cap with a black visor | dark charcoal high mandarin-collared tunic | jade-green |
| `europe-woman` | an original fictional Western European woman aged about 33, clearly adult, beautiful, fair skin with faint freckles, hazel-green eyes, auburn hair in a sleek low bun | a navy blue beret worn tilted to one side | navy blue officer's tunic with a stand collar | silver |
| `europe-man` | an original fictional Central European man aged about 42, clearly adult, handsome, light skin, grey-blue eyes, short dark-blond hair, a neatly trimmed short beard | a navy blue kepi-style cap with a flat top and a short black visor | navy blue officer's tunic with a stand collar | silver |
| `russia-woman` | an original fictional Slavic woman aged about 31, clearly adult, beautiful, pale skin, high cheekbones, pale grey-blue eyes, ash-blonde hair in a braided bun | a black garrison side cap with red piping, tilted slightly | black officer's tunic | crimson red |
| `russia-man` | an original fictional Slavic man aged about 41, clearly adult, handsome, fair skin, strong jaw, light stubble, pale blue eyes, short dark-brown hair | a black peaked officer's cap with a crimson red band and a black visor | black officer's tunic | crimson red |
| `us-woman` | an original fictional American woman aged about 33, clearly adult, beautiful, lightly tanned skin, green eyes, honey-blonde hair in a sleek low bun | a navy blue peaked officer's cap with a black visor edged in gold braid | navy blue dress uniform jacket over a white shirt and dark tie | gold |
| `us-man` | an original fictional American man aged about 42, clearly adult, handsome, tanned skin, square jaw, clean-shaven, brown eyes, short dark-brown hair with greying temples | an olive-drab peaked officer's cap with a black visor edged in gold braid | olive-drab dress uniform jacket over a light shirt and dark tie | gold |
| `africa-woman` | an original fictional Sub-Saharan African woman aged about 32, clearly adult, beautiful, deep brown skin, dark eyes, high cheekbones, natural black hair in neat braids gathered into a low bun | an olive-green beret with a thin gold trim band, worn tilted to one side | khaki-olive officer's tunic | gold |
| `africa-man` | an original fictional Sub-Saharan African man aged about 41, clearly adult, handsome, deep brown skin, dark eyes, strong jaw, close-cropped black hair, a neatly trimmed short beard | an olive-green beret with a thin gold trim band, worn tilted to one side | khaki-olive officer's tunic | gold |
| `southamerica-woman` | an original fictional Latin American woman aged about 32, clearly adult, beautiful, warm tan skin, dark brown eyes, full brows, long dark-brown wavy hair gathered into a low bun | a dark slate-grey peaked officer's cap with a black visor edged in gold braid | dark slate-grey officer's tunic with gold braid on the cuffs and a gold braided cord at the shoulder | gold |
| `southamerica-man` | an original fictional Latin American man aged about 40, clearly adult, handsome, warm tan skin, dark brown eyes, dark wavy hair combed back, a neatly trimmed short beard | a dark slate-grey peaked officer's cap with a black visor edged in gold braid | dark slate-grey officer's tunic with gold braid on the cuffs and a gold braided cord at the shoulder | gold |

<a id="romania-woman-caption"></a>
### `romania-woman` caption (verbatim)

Seed 7604, still run `20261020-093646-api`.

```json
{
 "aspect_ratio": "9:16",
 "high_level_description": "Head-and-upper-body portrait of an original fictional woman aged about 32, clearly adult, a beautiful Latin American woman with a subtle Slavic influence, warm olive skin, high cheekbones, cool light grey eyes, defined dark brows with one eyebrow slightly raised, glossy red lips, long glossy dark hair in a long sleek glossy fall with soft retro curls at the ends, partly under the cap, a sly knowing closed-lip half-smile, playful and ironic as if she is in on the joke, looking directly into the camera, wearing a black leather-look garrison side cap with red piping, tilted at a jaunty angle over her left brow, with a small fictional amber-gold crest of three stacked chevrons forming an arrowhead pinned on its front and a fitted black leather-look military tunic with red piping along the collar, placket and cuffs, black epaulettes trimmed in red, the collar unbuttoned and open at the throat down to the collarbones only, buttoned below, a small black shoulder patch with a fictional amber-gold crest of three stacked chevrons forming an arrowhead, no medals, no national insignia, retro Cold-War pin-up officer styling, sexy and confident yet classy and fully covered, black leather gloves, framed from mid-chest up and centred, in a retro Cold-War HQ bunker with propaganda-poster energy: a glowing holographic radar table light and a bold deep red banner with a large fictional amber-gold crest of three stacked chevrons forming an arrowhead behind her, a dramatic spotlight beam cutting through light haze, old brass analogue dials and a softly glowing green circular radar screen on the riveted bunker wall, a subtle holographic HUD glow of abstract lines and arcs with no characters, dramatic amber and red rim light from behind, cool teal fill light from the side, heightened saturated colour, deep shadows, stylized high-end video game character render, modern strategy game commander portrait, Unreal Engine 5 cinematic, detailed skin shader with subsurface scattering, slightly stylized proportions, crisp detail, polished game cinematic look, no text, no letters, no numbers, no logos, no flags, no real-world symbols, no hammer and sickle, no stars of any kind, no five-pointed stars",
 "compositional_deconstruction": {
  "background": "a retro Cold-War HQ bunker with propaganda-poster energy: a glowing holographic radar table light and a bold deep red banner with a large fictional amber-gold crest of three stacked chevrons forming an arrowhead behind her, a dramatic spotlight beam cutting through light haze, old brass analogue dials and a softly glowing green circular radar screen on the riveted bunker wall, a subtle holographic HUD glow of abstract lines and arcs with no characters",
  "elements": [
   {
    "type": "obj",
    "bbox": [
     120,
     150,
     640,
     850
    ],
    "desc": "an original fictional woman aged about 32, clearly adult, a beautiful Latin American woman with a subtle Slavic influence, warm olive skin, high cheekbones, cool light grey eyes, defined dark brows with one eyebrow slightly raised, glossy red lips, long glossy dark hair in a long sleek glossy fall with soft retro curls at the ends, partly under the cap, a sly knowing closed-lip half-smile, playful and ironic as if she is in on the joke, looking directly into the camera, wearing a black leather-look garrison side cap with red piping, tilted at a jaunty angle over her left brow, with a small fictional amber-gold crest of three stacked chevrons forming an arrowhead pinned on its front; face and eyes in sharp focus, an original face that resembles no real person"
   },
   {
    "type": "obj",
    "bbox": [
     600,
     50,
     1000,
     950
    ],
    "desc": "upper body in a fitted black leather-look military tunic with red piping along the collar, placket and cuffs, black epaulettes trimmed in red, the collar unbuttoned and open at the throat down to the collarbones only, buttoned below, a small black shoulder patch with a fictional amber-gold crest of three stacked chevrons forming an arrowhead, no medals, no national insignia, retro Cold-War pin-up officer styling, sexy and confident yet classy and fully covered, black leather gloves"
   },
   {
    "type": "obj",
    "bbox": [
     0,
     0,
     1000,
     1000
    ],
    "desc": "a retro Cold-War HQ bunker with propaganda-poster energy: a glowing holographic radar table light and a bold deep red banner with a large fictional amber-gold crest of three stacked chevrons forming an arrowhead behind her, a dramatic spotlight beam cutting through light haze, old brass analogue dials and a softly glowing green circular radar screen on the riveted bunker wall, a subtle holographic HUD glow of abstract lines and arcs with no characters, dramatic amber and red rim light from behind, cool teal fill light from the side, heightened saturated colour, deep shadows"
   }
  ]
 }
}
```

## Wan prompts

Three prompt variants were used as the motion direction was tuned during the batch. Each is written with pronoun placeholders. For a woman, `{pos}`=her, `{pro}`=she, `{Pro}`=She, `{obj}`=her; for a man, `{pos}`=his, `{pro}`=he, `{Pro}`=He, `{obj}`=him. The substituted text is byte-identical to the recorded prompt.

**Variant A** (`asia-woman`, `asia-man`, `europe-woman`, `europe-man`):

```
A cinematic strategy game commander portrait in a dim HQ command bunker. The commanding officer faces the camera with a calm, smouldering, confident gaze, upright posture, {pos} lips closed. Subtle idle animation only: slow steady breathing that gently lifts the chest and shoulders, {pro} blinks once slowly, {pos} head turns very slightly to one side and settles back to centre. The amber and teal rim light shimmers softly along {pos} cap and shoulders. Behind {obj} the glowing tactical map screen and the holographic HUD haze flicker faintly and drift slowly. Locked-off static camera, no camera movement, no zoom, no pan. {Pro} does not speak, does not talk, mouth stays closed, no hand gestures, no smile. Stable composition, consistent lighting, crisp detail.
```

**Variant B** (`russia-woman`, `russia-man`, `us-woman`):

```
A cinematic strategy game commander portrait in a dim HQ command bunker. The commanding officer faces the camera with a calm, smouldering, confident gaze, upright posture, {pos} lips closed. Subtle idle animation only: slow steady breathing that gently lifts the chest and shoulders, {pos} eyes stay open and alert, looking straight into the camera, {pro} blinks once quickly and naturally, {pos} head turns very slightly to one side and settles back to centre. The amber and teal rim light shimmers softly along {pos} cap and shoulders. Behind {obj} the glowing tactical map screen and the holographic HUD haze flicker faintly and drift slowly. Locked-off static camera, no camera movement, no zoom, no pan. {Pro} does not speak, does not talk, mouth stays closed, no hand gestures, no smile. Stable composition, consistent lighting, crisp detail.
```

**Variant C** (`us-man`, `africa-woman`, `africa-man`, `southamerica-woman`, `southamerica-man`):

```
A cinematic strategy game commander portrait in a dim HQ command bunker. The commanding officer faces the camera with a calm, alert, confident gaze with eyes wide open, upright posture, {pos} lips closed. Subtle idle animation only: slow steady breathing that gently lifts the chest and shoulders, {pos} eyes stay wide open and alert the whole time, looking straight into the camera, with a single brief natural blink, {pos} head turns very slightly to one side and settles back to centre. The amber and teal rim light shimmers softly along {pos} cap and shoulders. Behind {obj} the glowing tactical map screen and the holographic HUD haze flicker faintly and drift slowly. Locked-off static camera, no camera movement, no zoom, no pan. {Pro} does not speak, does not talk, mouth stays closed, no hand gestures, no smile. Stable composition, consistent lighting, crisp detail.
```

<a id="romania-woman-prompt"></a>
**`romania-woman` prompt** (verbatim):

```
A cinematic strategy game commander portrait in a retro Cold-War HQ bunker, a deep red banner with an amber-gold chevron crest behind her, brass dials and a glowing green radar screen on the riveted wall. The rebel commander faces the camera with a sly, knowing, closed-lip half-smile and a direct gaze; her expression stays the same. Subtle idle animation only: slow steady breathing that gently lifts the chest and shoulders, she blinks once slowly, her head turns very slightly to one side and settles back to centre. Her long loose dark hair stirs gently in a faint draft, a few strands lifting softly and settling. The amber and red rim light shimmers softly along her cap, hair and the black leather of her shoulders. The red banner ripples very slightly, the green radar screen and the holographic haze flicker faintly. Locked-off static camera, no camera movement, no zoom, no pan. She does not speak, does not talk, mouth stays closed, no hand gestures. Stable composition, consistent lighting, crisp detail.
```

**Negative prompt** (all 13; the dashboard's default for Wan):

```
Bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, static, overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, still picture, messy background, three legs, many people in the background, walking backwards
```

## Loop and encode

The same pipeline ran for all 13, with ffmpeg 6.1.1. The only per-asset input is the crop's `y` offset (`CY`, in the table above). The crop window is 628×838 at x=38 in the 704×1280 clip for every asset. The steps:

1. Take frames 0..120 (all 121) and crop.
2. Upscale to 480×640, stretch timestamps ×1.25 (0.8× speed), then use `minterpolate` (mci / aobmc / bidir) to re-synthesise 24 fps. This makes the forward pass F = 149 frames. Then downscale to 240×320.
3. Ping-pong it: append the forward pass reversed with both endpoints dropped (frames F-2..1), so no frame repeats at either turn. That gives 2F-2 = 296 frames = 12.33 s, and the last frame flows into the first.
4. Encode VP9 WebM and H.264 MP4, with GOP = the loop length. Take the JPEG poster from frame 0.

Each video has a 400,000-byte budget. The pipeline steps CRF up (VP9 32→35→…→44, x264 21→23→…→29) only when a video is over budget. No asset needed that, so all 13 use **VP9 CRF 32** and **x264 CRF 21**.

```sh
# placeholders: RAW = the Wan clip (704x1280, 121 frames, 24 fps), CY = crop y from the table,
#               TMP = scratch dir, OUT/<id> = output basename
# 1) forward pass: crop, 2x-size motion-compensated slow-down to 0.8x, scale to 240x320, lossless
ffmpeg -y -i RAW -vf "select='lte(n\,120)',setpts=N/24/TB,crop=628:838:38:CY,scale=480:640:flags=lanczos,\
setpts=1.25*PTS,minterpolate=fps=24:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,\
scale=240:320:flags=lanczos,format=yuv420p" -an -c:v libx264 -qp 0 -preset veryfast TMP/fwd.mp4

# F = frame count of TMP/fwd.mp4 (149 here):
#   ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of csv=p=0 TMP/fwd.mp4
# 2) ping-pong: forward + reversed middle (both endpoints dropped) -> 2F-2 = 296 frames, lossless
ffmpeg -y -i TMP/fwd.mp4 -filter_complex \
  "[0:v]split[a][b];[b]reverse,trim=start_frame=1:end_frame=F-1,setpts=N/24/TB[r];\
[a][r]concat=n=2:v=1,setpts=N/24/TB,fps=24[v]" \
  -map "[v]" -an -c:v libx264 -qp 0 -preset veryfast TMP/loop.mp4

# 3) web encodes (G = 296 = frame count of TMP/loop.mp4)
ffmpeg -y -i TMP/loop.mp4 -an -c:v libvpx-vp9 -b:v 0 -crf 32 -row-mt 1 \
  -deadline good -cpu-used 1 -pix_fmt yuv420p -g 296 OUT/<id>.webm
ffmpeg -y -i TMP/loop.mp4 -an -c:v libx264 -profile:v main -level 3.0 \
  -preset slow -crf 21 -x264-params ipratio=1.0:pbratio=1.0:mbtree=0 -pix_fmt yuv420p -g 296 \
  -movflags +faststart OUT/<id>.mp4

# 4) poster = first frame of the loop
ffmpeg -y -i TMP/loop.mp4 -frames:v 1 -q:v 3 OUT/<id>.jpg
```

In `end_frame=F-1`, F is the measured frame count, so with F = 149 the filter is `end_frame=148`.

Seam check: the PSNR between the delivered MP4's last and first frames is 37.8 to 42.7 dB across the 13 loops.

## Licences

Verified on 2026-10-20 from the model repositories' own licence files and model cards.

### Ideogram 4 (stills): non-commercial. Shipping these outputs in a commercial product is NOT permitted

- **Weights used:** `ideogram-ai/ideogram-4-nf4-diffusers`, recorded as the model on every still run above.
- **Licence:** *Ideogram Non-Commercial Model Agreement*, "Last Updated: June 3, 2026". The model card metadata reads `license: other`, `license_name: ideogram-4-non-commercial`.
- **Sources:** https://huggingface.co/ideogram-ai/ideogram-4-nf4-diffusers/blob/main/LICENSE.md and the model card at https://huggingface.co/ideogram-ai/ideogram-4-nf4-diffusers. The same text is in https://huggingface.co/ideogram-ai/ideogram-4-nf4/blob/main/LICENSE.md (byte-identical) and in https://github.com/ideogram-oss/ideogram4/blob/main/model_licenses/LICENSE-IDEOGRAM-4-NON-COMMERCIAL, which differs only in one URL in the attribution notice. The GitHub repo's top-level Apache-2.0 `LICENSE.md` covers the inference code, not the weights.
- **What it says** (verbatim):
  - §2 Grant of Rights: "you are only authorized to exercise the rights under this Agreement for Non-Commercial Purposes only, and may not exercise any of the rights under this Agreement for other purposes unless or until Company otherwise expressly grants you such rights in a separate agreement".
  - §1(d) Non-Commercial Purposes: "(i) use that does not directly or indirectly generate revenue and is not otherwise intended for or directed towards commercial advantage or monetary compensation, (ii) use by a for-profit entity solely for testing, evaluation, or research and development in a “non-production environment” (an environment that is not deployed in live systems, customer-facing applications or any other environment beyond internal development, testing or prototyping), […] Without limiting the foregoing, any use that involves training, fine tuning, or distilling AI models for commercial use or that involves generating Output to include in, or to advertise or promote, revenue-generating products or services, in each case, is not a Non-Commercial Purpose."
  - §7: "We claim no rights in outputs you generate using the Model. You are responsible for outputs and their subsequent uses."
  - §4 also requires compliance with Ideogram's Acceptable Use Policy and, "To the extent required under applicable law", an AI-generation disclosure on Output.
- **Verdict: CLEARLY FORBIDDEN for a commercial product.** Generating output to include in a revenue-generating product falls outside the grant by the agreement's own definition. §7 means Ideogram claims no ownership of the outputs. It does not widen the grant: making the outputs is itself "use of the Model" and is limited to Non-Commercial Purposes. These outputs were made to ship in a customer-facing product UI, not a "non-production environment". That rules out §1(d)(ii). §1(d)(i) is at best doubtful for a product whose site lists a managed paid tier ("Pro … coming soon"). The only path the agreement offers is a separate written grant from Ideogram, Inc.
- **Scope:** all 13 assets. Each Wan clip is conditioned on its Ideogram still, and the poster JPEG is the loop's first frame, so no file here is free of an Ideogram output.
- **Confidence:** high on the text and its meaning. This is not legal advice. The agreement says Ideogram may change it at any time, so re-check the date above before relying on this.

- **Decision (2026-10-20):** the operator chose to ship these assets as they are (option C on the implementation PR #1535, in the operator's own comment there), knowing the verdict above. Recorded here so it is not rediscovered as an open question. Regenerating the stills with a commercially licensed model (option A) stays the remedy if this is revisited.

### Wan 2.2 TI2V-5B (motion): Apache-2.0, permitted

- **Weights used:** `Wan-AI/Wan2.2-TI2V-5B-Diffusers` at revision `b8fff73`, as recorded on every Wan run. This is the repository's current head, `b8fff7315c768468a5333511427288870b2e9635`.
- **Licence:** Apache License 2.0. The model card metadata reads `license: apache-2.0`.
- **Sources:** https://huggingface.co/Wan-AI/Wan2.2-TI2V-5B-Diffusers (model card, "License Agreement" section). The card links a `LICENSE.txt` that this Diffusers repo does not contain. The full Apache-2.0 text is at https://github.com/Wan-Video/Wan2.2/blob/main/LICENSE.txt.
- **What the card says** (verbatim): "The models in this repository are licensed under the Apache 2.0 License. We claim no rights over the your generated contents, granting you the freedom to use them while ensuring that your usage complies with the provisions of this license. You are fully accountable for your use of the models, which must not involve sharing any content that violates applicable laws, causes harm to individuals or groups, disseminates personal information intended for harm, spreads misinformation, or targets vulnerable populations."
- **Verdict: CLEARLY PERMITTED** for the Wan step on its own. Apache-2.0 has no field-of-use restriction, and the card disclaims rights in outputs, subject to the content conditions quoted above. This does not cure the Ideogram restriction on the input stills (see above).

### Tools

ffmpeg (libx264, libvpx-vp9, minterpolate) only transcoded the clips. Its licence does not attach to the encoded media.

## Checks

Manual visual review per asset, done on the shipped posters and loops. "No evident likeness" is what a visual review can say: the prompts name no person, and no face resembled anyone known to the reviewer. It is not a proof. Insignia are a fictional chevron crest; ribbon bars are generic colour blocks; Ileana Lupu's backdrop banner is a fictional red-and-gold chevron, not a flag. No legible text on any asset.

| id | no real-person likeness | no real flag/insignia/text | reviewer | date |
|---|---|---|---|---|
| `asia-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `asia-man` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `europe-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `europe-man` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `russia-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `russia-man` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `us-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `us-man` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `africa-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `africa-man` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `southamerica-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `southamerica-man` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
| `romania-woman` | no evident likeness | none | agent review (claude session f98ef572) | 2026-10-20 |
