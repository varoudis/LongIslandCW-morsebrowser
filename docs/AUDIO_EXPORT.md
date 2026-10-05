# Audio Export (MP3 with voice)

`tools/morse_audio_export.py` turns any lesson and preset from the practice page into an MP3 (or WAV) file. You don't need to open the site. Unlike the site's **Output Options › Audio File** button, which saves Morse only, the exported file also includes the spoken parts. They follow the same rules as live playback.

Related docs:

- [DEVELOPER_GUIDE.md](./DEVELOPER_GUIDE.md)
- [SPEED_RACER.md](./SPEED_RACER.md)

## What it reproduces

The script reads lessons, word files and presets straight from this repository (`src/wordfilesconfigs/`, `src/wordfiles/`, `src/presets/`, `src/configs/`). It then replays the web app's playback rules offline:

- Character / Farnsworth speed, extra word space, PRE padding, card wait
- Repeats and repeat spacing, Speed Intervals, Speed Racer (variations, replay, Speak)
- Random group generation (lesson letters, Override size, Override time), shuffle
- Voice modes:
  - **Voice** (spoken after the Morse)
  - **Voice First** (spoken before the Morse)
  - Spell on/off, Delay Before / Delay After, Last Only, voice buffer
  - Speed Racer recap

The script checks voice against each preset's own setting. Presets with Voice off give a Morse-only file and never call a TTS engine.

| Difference from the site | Why |
|---|---|
| **Arm Recap** presets (e.g. VET, VST, Default 12/8) speak the recap after all the Morse. | The site waits for the Voice Recap button, and a file has no button. `--arm-recap none` leaves the recap out. |
| Spelled cards are sent to TTS as `R. E. A.` instead of `r e a`. | Neural voices run plain letters together. The site already does this for Speed Racer recaps. |
| Noise and Loop are not exported. | Not useful in a file. |

## Requirements

- Python 3.9+ (standard library only)
- `ffmpeg` on `PATH` for MP3 output (`--format wav` works without it)
- A speech engine for presets with Voice on, either:
  - an OpenAI-style TTS server (`POST /v1/audio/speech` returning WAV), set with `--tts-url`; or
  - macOS built-in voices via `say` (`--tts-engine say`)

Run the commands from the repository root.

## Quick start

```bash
# Browse the catalog (same TYPE / CLASS / CONTENT / LESSON / PRESETS as the site)
tools/morse_audio_export.py list
tools/morse_audio_export.py list --class BC1
tools/morse_audio_export.py list --class BC1 --group LCD

# Render one lesson with one preset
tools/morse_audio_export.py render --class BC1 --group LCD --lesson "LCD PSG TIN REA" \
  --preset Recognition --tts-engine say --say-voice Daniel

# Same lesson from a site deep link
tools/morse_audio_export.py render --tts-engine say --link \
  "https://longislandcw.github.io/morsebrowser/?selectedClass=BC1&selectedGroup=LCD&selectedLesson=LCD%20PSG%20TIN%20REA&selectedPreset=Recognition"

# Every preset of a lesson, or every lesson x preset of a content group
tools/morse_audio_export.py render --class BC1 --group LCD --lesson "LCD PSG" --preset all
tools/morse_audio_export.py render --class BC1 --group LCD --lesson all --preset all

# Check the timeline first (no TTS calls, no audio written)
tools/morse_audio_export.py render --class BC1 --group LCD --lesson "LCD PSG" --preset Recognition --dry-run
```

By default, files go to `morse_audio/` (git-ignored) and get a name built from class, content, lesson and preset. `-o` sets an exact path for a single render.

## Choosing a lesson

| Option | Site picker |
|---|---|
| `--type` | TYPE (default `STUDENT`) |
| `--class` | CLASS, e.g. `BC1`, `INT 1`, `OVERLEARN` |
| `--group` | CONTENT, e.g. `LCD` |
| `--lesson` | LESSON display name, or `all` |
| `--preset` | PRESETS display name, `Your Settings`, or `all` (default: first preset) |

Names match without regard to case. Use `list` to see the exact names.

Your own text works too:

```bash
tools/morse_audio_export.py render --text "CQ CQ DE K1ABC" --set voiceEnabled=true
tools/morse_audio_export.py render --text-file mytext.txt --preset-file LICWSettings.json
```

`LICWSettings.json` is the file the site's **Save Settings** button writes.

## Changing settings

Any setting can be overridden with `--set key=value`, using the same keys as the preset JSON files in `src/presets/configs/`. The ones you will use most:

| Goal | Option |
|---|---|
| Character / effective speed | `--set syncWpm=false --set wpm=14 --set fwpm=8` |
| Word space (site: WORD SPACE) | `--set xtraWordSpaceDits=2` |
| Letters per random group, fixed | `--group-size 3` |
| Letters per random group, range | `--set overrideSize=true --set syncSize=false --set overrideSizeMin=2 --set overrideSizeMax=3` |
| Longer random lesson (site: Override time) | `--minutes 10` |
| Pause between Morse and voice | `--set voiceThinkingTime=2` (seconds) |
| Pause after voice, before next card | `--set voiceAfterThinkingTime=2` (seconds) |
| Extra pause between cards | `--set cardSpace=1` (seconds) |
| Repeats per card | `--set numberOfRepeats=2` |
| Voice on / off | `--set voiceEnabled=true` / `--no-voice` |
| Voice First, or say instead of spell | `--set speakFirst=true`, `--set voiceSpelling=false` |
| Same random groups again | `--seed 42` |

Notes:

- **Farnsworth needs unlinked speeds.** With `syncWpm` on (most presets), `fwpm` follows `wpm`, so set `syncWpm=false` to use a slower effective speed.
- **Exact thinking time.** When Voice is on, the last word space of a card is dropped, as on the site. The silence between Morse and voice is exactly `voiceThinkingTime`.
- **What `--minutes` applies to.** It controls lessons that generate random groups. Lessons built from a fixed word list always play the whole list. Voice and pauses make the file longer than the Morse time.

## Speech engines and the clip cache

**TTS server** (default engine): `--tts-url http://host:port`, `--tts-voice NAME`. The server's `/voices` endpoint lists the voices, if it has one. `--tts-concurrency` sets how many requests run in parallel.

**macOS `say`**: `--tts-engine say --say-voice Daniel --say-rate 140`. Spelled letters are separated with `[[slnc 250]]`; change the pause with `--say-letter-gap` (ms, `0` = none). `say -v '?'` lists the installed voices.

Clips are cached next to the output in `tts_cache/<voice>/`, e.g. `tts_cache/Aiden/` or `tts_cache/say-Daniel_r140_gap250/`. Files are named after the spoken text (`E.A.wav`, `very.wav`), and `index.json` maps each exact phrase to its file. Later renders into the same folder reuse the clips and only synthesize new phrases. `--cache-dir` points several output folders at one shared cache.

## Output

| Option | Default |
|---|---|
| `--format` | `mp3` (`wav` also available) |
| `--bitrate` | `96k` |
| `--sample-rate` | `24000` |
| `--tone-db` / `--voice-db` | `-9` / `-2` dBFS peak |
| `--cues` | off; writes `<file>.cues.txt`, a timestamped list of every card, Morse play and spoken phrase |

## Example: BC1 recognition practice set

This builds 28 files: each BC1 LCD, HOF and UWB lesson listed below, at 14/8 wpm with word space 2, 10 minutes of Morse, a 2 s thinking time and the macOS Daniel voice. There are two versions of each lesson, one with 2–3 letter groups and one with single characters.

One file:

```bash
tools/morse_audio_export.py render \
  --class BC1 --group HOF --lesson "HOF LCD" \
  --preset Recognition \
  --set syncWpm=false --set wpm=14 --set fwpm=8 \
  --set xtraWordSpaceDits=2 \
  --set overrideSize=true --set syncSize=false \
  --set overrideSizeMin=2 --set overrideSizeMax=3 \
  --minutes 10 \
  --set voiceThinkingTime=2 \
  --tts-engine say --say-voice Daniel \
  --cues \
  -o morse_audio/BC1_HOF-LCD_14-8_2-3letters_ws2_10min_Daniel_think2.mp3
```

For single characters, replace the four `overrideSize` / `syncSize` settings with `--group-size 1`.

The whole set (zsh or bash):

```bash
render() { G="$1"; L="$2"; SZ="$3"
  F="morse_audio/BC1_$(echo "$L" | tr ' ' '-')_14-8_${SZ}_ws2_10min_Daniel_think2"
  if [ "$SZ" = 1letter ]; then SIZE=(--group-size 1)
  else SIZE=(--set overrideSize=true --set syncSize=false --set overrideSizeMin=2 --set overrideSizeMax=3); fi
  tools/morse_audio_export.py render --class BC1 --group "$G" --lesson "$L" --preset Recognition \
    --set syncWpm=false --set wpm=14 --set fwpm=8 --set xtraWordSpaceDits=2 "${SIZE[@]}" \
    --minutes 10 --set voiceThinkingTime=2 --tts-engine say --say-voice Daniel --cues -o "$F.mp3"; }

for SZ in 2-3letters 1letter; do
  for L in "LCD PSG" "LCD PSG TIN" "LCD PSG TIN REA"; do render LCD "$L" $SZ; done
  for L in "HOF" "HOF LCD" "HOF LCD PSG" "HOF LCD PSG TIN" "HOF LCD PSG TIN REA"; do render HOF "$L" $SZ; done
  for L in "UWB" "UWB HOF" "UWB HOF LCD" "UWB HOF LCD PSG" "UWB HOF LCD PSG TIN" "UWB HOF LCD PSG TIN REA"; do render UWB "$L" $SZ; done
done
```

Size options are kept in an array so that zsh, which does not split unquoted variables into words, passes them as separate arguments.
