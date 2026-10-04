#!/usr/bin/env python3
"""
Render LICW Morse Practice Page lessons to MP3/WAV without opening the site.

The script reads the lesson catalog, word files and presets straight from this
repository (src/wordfilesconfigs, src/wordfiles, src/presets, src/configs) and
replays the web app's playback rules offline: card repeats and repeat spacing,
PRE padding, card wait, Farnsworth timing, Speed Intervals, Speed Racer, and the
voice modes (Voice after each card, Voice First, Voice Buffer, Last Only,
Speed Racer recap, Arm Recap). Speech comes from an OpenAI-style TTS server
(`POST /v1/audio/speech`), cached on disk so repeated words are fetched once.

Selection mirrors the site's LICW Lessons pickers / deep links:
TYPE (--type), CLASS (--class), CONTENT (--group), LESSON (--lesson),
PRESETS (--preset). Values match case-insensitively. Omitting --preset picks
the first preset of the class/content set, like the site does.

Examples
  # Browse the catalog
  tools/morse_audio_export.py list
  tools/morse_audio_export.py list --class BC1
  tools/morse_audio_export.py list --class BC1 --group REA

  # One file
  tools/morse_audio_export.py render --class BC1 --group REA --lesson "REA UWB" \
      --preset "Voice Echo Trainer" -o rea_uwb.mp3

  # Same thing from a site deep link
  tools/morse_audio_export.py render --link \
      "https://longislandcw.github.io/morsebrowser/?selectedClass=BC1&selectedGroup=REA&selectedLesson=REA%20UWB"

  # Every preset of one lesson, or every lesson x preset of a content group
  tools/morse_audio_export.py render --class BC1 --group REA --lesson "REA UWB" --preset all
  tools/morse_audio_export.py render --class BC1 --group REA --lesson all --preset all

  # Your own text with settings saved from the site (Save Settings -> LICWSettings.json)
  tools/morse_audio_export.py render --text-file mytext.txt --preset-file LICWSettings.json

  # Override any setting by its preset key
  tools/morse_audio_export.py render ... --set wpm=20 --set voiceEnabled=false

Requires Python 3.9+ and ffmpeg (for MP3). No third-party Python packages.
"""

import argparse
import array
import concurrent.futures
import hashlib
import json
import math
import operator
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parent.parent
DEFAULT_TTS_URL = 'http://100.100.0.25:18084'
DEFAULT_TTS_VOICE = 'Aiden'
YOUR_SETTINGS = 'Your Settings'

# Keys the site ignores when a preset is applied (morseLessonPlugin.setPresetSelected).
PRESET_KEY_BLACKLIST = {'ditFrequency', 'dahFrequency', 'syncFreq', 'cardFontPx', 'preSpace', 'volume', 'voiceVolume'}


# ---------------------------------------------------------------------------
# JS-compatible helpers
# ---------------------------------------------------------------------------

def js_float(v, default=math.nan):
    """parseFloat semantics: leading numeric prefix, else NaN."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r'\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?', str(v))
    return float(m.group(0)) if m else default


def js_int(v, default=0):
    """parseInt semantics (base 10)."""
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return int(v) if math.isfinite(v) else default
    m = re.match(r'\s*[+-]?\d+', str(v))
    return int(m.group(0)) if m else default


def js_round(x):
    return math.floor(x + 0.5)


def to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(v)


def load_json(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Morse text handling (ports of morseStringUtils.ts, wordInfo.ts, morse-pro.js)
# ---------------------------------------------------------------------------

TEXT2MORSE = {
    'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..', 'E': '.', 'F': '..-.', 'G': '--.', 'H': '....',
    'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..', 'M': '--', 'N': '-.', 'O': '---', 'P': '.--.',
    'Q': '--.-', 'R': '.-.', 'S': '...', 'T': '-', 'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
    'Y': '-.--', 'Z': '--..', '1': '.----', '2': '..---', '3': '...--', '4': '....-', '5': '.....',
    '6': '-....', '7': '--...', '8': '---..', '9': '----.', '0': '-----', '.': '.-.-.-', ',': '--..--',
    ':': '---...', '?': '..--..', "'": '.----.', '-': '-....-', '/': '-..-.', '(': '-.--.', ')': '-.--.-',
    '"': '.-..-.', '@': '.--.-.', '=': '-...-', '&': '.-...', '+': '.-.-.', '!': '-.-.--', ' ': '/',
    '<AA>': '.-.-', '<AR>': '.-.-.', '<AS>': '.-...', '<BK>': '-...-.-', '<BT>': '-...-',
    '<CL>': '-.-..-..', '<CT>': '-.-.-', '<DO>': '-..---', '<KN>': '-.--.', '<SK>': '...-.-',
    '<VA>': '...-.-', '<SN>': '...-.', '<VE>': '...-.', '<SOS>': '...---...',
}


def text2morse(text):
    text = re.sub(r'\s+', ' ', text.upper().strip())
    codes = []
    i = 0
    while i < len(text):
        m = re.match(r'<...?>', text[i:]) if text[i] == '<' else None
        tok = m.group(0) if m else text[i]
        i += len(tok)
        codes.append(TEXT2MORSE.get(tok, '#'))
    return ' '.join(codes)


_REPL_RE = re.compile(r'(?![|{}.,:?\\\-/()"@=&+!<>\r\n])\W', re.ASCII)


def do_replacements(s):
    s = (s.replace('Ø', '0').replace('’', '').replace('‘', '').replace("'", '')
         .replace('%', 'pct'))
    return _REPL_RE.sub(' ', s)


class Wordifier:
    def __init__(self, entries):
        # (pattern, length of characters, replacement, onlyAlone, overrideSpell) in file order
        self.entries = [(re.compile(re.escape(w['characters']), re.IGNORECASE), len(w['characters']),
                         '|' + w['replacement'] + '|', bool(w.get('onlyAlone')), bool(w.get('overrideSpell')))
                        for w in entries]

    def wordify(self, s, spell_overrides_only=False):
        fixed = s.replace('\r', '').replace('\n', '')
        for pat, n_chars, rep, only_alone, override_spell in self.entries:
            if spell_overrides_only and not override_spell:
                continue
            if not only_alone or n_chars == len(fixed):
                fixed = pat.sub(lambda _m, rep=rep: rep, fixed)
        return fixed


class WordInfo:
    _PIECE_SPLIT = re.compile(r' (?![^{]*})')

    def __init__(self, s, wordifier):
        self.raw_word = s
        self.pieces = self._PIECE_SPLIT.split(s)
        self.wordifier = wordifier

    @staticmethod
    def parse_override(p):
        if '{' not in p:
            return None
        t = p.strip()
        inner = t[1:-1] if (t.startswith('{') and t.endswith('}')) else re.sub(r'[{}]', '', t)
        parts = inner.split('|')
        morse = do_replacements(parts[0])
        speech = do_replacements(parts[1]) if len(parts) > 1 else None
        gid = parts[2].strip() if len(parts) > 2 else None
        group_id = int(gid) if gid is not None and re.fullmatch(r'-?\d+', gid) else None
        return morse, speech, group_id

    @property
    def display_word(self):
        out = []
        for p in self.pieces:
            o = self.parse_override(p)
            out.append(o[0] if o else do_replacements(p))
        return ' '.join(out)

    def group_id(self):
        for p in self.pieces:
            o = self.parse_override(p)
            if o and o[2] is not None:
                return o[2]
        return None

    def speak_text(self, force_spelling):
        out = []
        for p in self.pieces:
            o = self.parse_override(p)
            if not o:
                base = do_replacements(p) + '\n'
                if not force_spelling:
                    out.append(self.wordifier.wordify(base))
                else:
                    chars = base.replace('>', '').replace('<', '')
                    pre = ' '.join(self.wordifier.wordify(c, True) for c in chars)
                    pre = re.sub(r'(\d) e (\d)', r'\1,e,\2', pre, flags=re.IGNORECASE)
                    out.append(pre)
            else:
                morse, speech, _ = o
                if not force_spelling:
                    out.append(speech if speech is not None else morse)
                else:
                    out.append(' '.join(morse))
        return ' '.join(out) + '\n'


def get_words(text, newline_chunking, wordifier):
    r = do_replacements(text)
    parts = re.split(r'\n(?![^{]*})', r) if newline_chunking else re.split(r' (?![^{]*})', r)
    return [WordInfo(w, wordifier) for w in parts if re.sub(r'\s', '', w)]


def prep_phrase(phrase):
    """prepPhraseToSpeakForFinal + the lower-casing done in initMorseVoiceInfo."""
    p = phrase.replace('|', ' ')
    p = re.sub(r'\WV\W', ' VEE ', p)
    p = re.sub(r'^V\W', ' VEE ', p)
    p = re.sub(r'\WV$', ' VEE ', p)
    return p.lower()


def format_spelled_recap(speak_text):
    """voicePlayback.formatSpelledRecapPhrase."""
    letters = [t for t in re.sub(r'[\r\n|]+', ' ', speak_text).strip().split() if t]
    if not letters:
        return ''
    if len(letters) == 1:
        return letters[0]
    return ' '.join(re.sub(r'\.+$', '', l) + '.' for l in letters)


def tts_ready_text(final_phrase, spelled):
    """Make a phrase friendlier to a neural TTS without changing what is said.

    The browser speaks spelled cards as "r e a"; neural engines tend to slur
    that, so letters are period-paced and upper-cased ("R. E. A.") the same way
    the site already does for Speed Racer recaps.
    """
    text = re.sub(r'\s+', ' ', final_phrase).strip()
    if not spelled or not text:
        return text
    text = text.replace('question mark', 'question\0mark')
    toks = []
    for t in text.split(' '):
        t = t.replace('\0', ' ').rstrip('.')
        if not t:
            continue
        toks.append(t.upper() if len(t) == 1 else t)
    return '. '.join(toks) + '.'


# ---------------------------------------------------------------------------
# Timing (ports of UnitTimingsAndMultipliers / MorseTimingCalculator)
# ---------------------------------------------------------------------------

def unit_ms(wpm, fwpm):
    eff = min(fwpm, wpm)
    dit_s = 60.0 / (50 * wpm)
    fw_s = ((60.0 / eff) - 31 * dit_s) / 19
    return dit_s * 1000, fw_s * 1000


def morse_play(word, wpm, fwpm, dit_freq, dah_freq, pre_ms, xtra_dits, trim_last_wordspace):
    """One player.play(): returns (tones[(start_ms, dur_ms, freq)], end_ms)."""
    unit, fw = unit_ms(wpm, fwpm)
    morse_words = [w.strip() for w in text2morse(word).split('/')]
    t = pre_ms
    tones = []
    for mw in morse_words:
        if t > 0:
            t += fw * 7
        last = ''
        for ch in mw:
            if ch in '.-':
                if last in ('.', '-'):
                    t += unit
                dur = unit if ch == '.' else unit * 3
                tones.append((t, dur, dit_freq if ch == '.' else dah_freq))
                t += dur
            elif ch == ' ':
                t += fw * 3
            last = ch
    end = t + (0 if trim_last_wordspace else fw * 7 + xtra_dits * fw)
    return tones, end


def estimate_ms(text, wpm, fwpm, xtra_word_space_dits):
    """MorseStringToWavBuffer.estimatePlayTime(...).timeCalcs.totalTime."""
    unit, fw = unit_ms(wpm, fwpm)
    morse = text2morse(do_replacements(text))
    words = morse.split('/')
    word_spaces = len(words) - 1
    inter = intra = dits = dahs = 0
    for w in words:
        chars = w.strip().split(' ')
        inter += len(chars) - 1
        for c in chars:
            intra += len(c) - 1
            dits += c.count('.')
            dahs += c.count('-')
    xtra = (js_int(xtra_word_space_dits) - 1) * 7
    return (dits * unit + dahs * 3 * unit + intra * unit + inter * 3 * fw
            + word_spaces * 7 * fw + word_spaces * xtra * fw)


# ---------------------------------------------------------------------------
# Settings (cookie/preset handler semantics from the view-model classes)
# ---------------------------------------------------------------------------

class Settings:
    # Direct MorseViewModel observables (set as-is by loadCookiesOrDefaults).
    DIRECT = {'xtraWordSpaceDits', 'volume', 'preSpace', 'cardSpace', 'numberOfRepeats', 'trailReveal',
              'trailPreDelay', 'trailPostDelay', 'trailFinal', 'riseTimeConstant', 'decayTimeConstant',
              'riseMsOffset', 'decayMsOffset'}

    def __init__(self):
        # Observable initial values from the constructors.
        self.d = {
            'xtraWordSpaceDits': 0, 'volume': 0, 'preSpace': 0, 'cardSpace': 0, 'numberOfRepeats': 0,
            'trailReveal': False, 'trailPreDelay': 0, 'trailPostDelay': 0, 'trailFinal': 1,
            'riseTimeConstant': 0.001, 'decayTimeConstant': 0.001, 'riseMsOffset': 1.5, 'decayMsOffset': 1.5,
            # speed
            'syncWpm': True, 'speedInterval': False, 'intervalTimingsText': '', 'intervalWpmText': '',
            'intervalFwpmText': '', 'speedRacerEnabled': False, 'speedRacerMultipliers': '1.5, 1.35, 1.175, 1.0',
            'speedRacerFinalPlay': True, 'speedRacerSpeakBeforeReplay': True, 'speedRacerKeepFwpm': True,
            # frequency
            'syncFreq': True, 'ditFrequency': 500, 'dahFrequency': 500,
            # misc
            'keepLines': False,
            # lessons
            'stickySets': '', 'ifStickySets': False, 'overrideSize': False, 'overrideSizeMin': 3,
            'overrideSizeMax': 3, 'syncSize': True, 'shuffleIntraGroup': False, 'isShuffledSet': False,
            # voice
            'voiceEnabled': False, 'voiceSpelling': True, 'voiceThinkingTime': 0, 'voiceAfterThinkingTime': 0,
            'voiceVolume': 10, 'voiceLastOnly': False, 'voiceRecap': False, 'voiceBufferMaxLength': 1,
            'speakFirst': False, 'speakFirstAdditionalWordspaces': 0,
        }
        self.true_wpm = 12
        self.true_fwpm = 12

    def apply(self, pairs, blacklist=()):
        """Apply [(key, value)] the way MorseCookies.loadCookiesOrDefaults does."""
        pairs = [(k, v) for k, v in pairs if k not in blacklist]
        given = dict(pairs)
        d = self.d
        for k, v in pairs:
            if k in self.DIRECT:
                if k == 'xtraWordSpaceDits' and js_int(v) == 0:
                    v = 1
                d[k] = v

        # SpeedSettings.handleCookies
        if 'syncWpm' in given:
            d['syncWpm'] = to_bool(given['syncWpm'])
        if 'wpm' in given:
            w = js_int(given['wpm'])
            self.true_wpm = w
            if d['syncWpm'] or w < self.true_fwpm:
                self.true_fwpm = w
        if 'fwpm' in given:
            f = js_int(given['fwpm'])
            if f <= self.true_wpm:
                self.true_fwpm = f
        if 'speedInterval' in given:
            d['speedInterval'] = to_bool(given['speedInterval'])
            if d['speedInterval'] and d['speedRacerEnabled']:
                d['speedRacerEnabled'] = False
        for k in ('intervalTimingsText', 'intervalWpmText', 'intervalFwpmText', 'speedRacerMultipliers'):
            if k in given:
                d[k] = str(given[k])
        if 'speedRacerEnabled' in given:
            d['speedRacerEnabled'] = to_bool(given['speedRacerEnabled'])
            if d['speedRacerEnabled'] and d['speedInterval']:
                d['speedInterval'] = False
        for k in ('speedRacerFinalPlay', 'speedRacerSpeakBeforeReplay', 'speedRacerKeepFwpm'):
            if k in given:
                d[k] = to_bool(given[k])

        # FrequencySettings.handleCookies
        if 'syncFreq' in given:
            d['syncFreq'] = to_bool(given['syncFreq'])
        if 'ditFrequency' in given:
            d['ditFrequency'] = js_int(given['ditFrequency'])
            if d['syncFreq']:
                d['dahFrequency'] = d['ditFrequency']
        if 'dahFrequency' in given:
            d['dahFrequency'] = js_int(given['dahFrequency'])

        # MiscSettings
        if 'keepLines' in given:
            d['keepLines'] = to_bool(given['keepLines'])

        # MorseLessonPlugin.handleCookies
        if 'stickySets' in given:
            d['stickySets'] = str(given['stickySets'])
        for k in ('ifStickySets', 'overrideSize', 'shuffleIntraGroup', 'isShuffledSet'):
            if k in given:
                d[k] = to_bool(given[k])
        if 'overrideSizeMin' in given:
            d['overrideSizeMin'] = js_int(given['overrideSizeMin'], 3)
            if d['syncSize']:
                d['overrideSizeMax'] = d['overrideSizeMin']
        if 'overrideSizeMax' in given:
            v = js_int(given['overrideSizeMax'], 3)
            if v >= d['overrideSizeMin']:
                d['overrideSizeMax'] = v
        if 'syncSize' in given:
            d['syncSize'] = to_bool(given['syncSize'])

        # MorseVoice.handleCookies
        if 'voiceEnabled' in given:
            was = d['voiceEnabled']
            d['voiceEnabled'] = to_bool(given['voiceEnabled'])
            if was and not d['voiceEnabled']:
                d['speakFirst'] = False  # turnOffSpeakFirstWithVoiceOff
                if d['speedRacerEnabled'] and d['speedRacerSpeakBeforeReplay']:
                    d['speedRacerSpeakBeforeReplay'] = False
        for k in ('voiceSpelling', 'voiceLastOnly', 'voiceRecap', 'speakFirst'):
            if k in given:
                d[k] = to_bool(given[k])
        for k in ('voiceThinkingTime', 'voiceAfterThinkingTime', 'voiceVolume'):
            if k in given:
                d[k] = given[k]
        if 'voiceBufferMaxLength' in given:
            d['voiceBufferMaxLength'] = js_int(given['voiceBufferMaxLength'], 1)
        if 'speakFirstRepeats' in given:
            d['numberOfRepeats'] = js_int(given['speakFirstRepeats'])
        if 'speakFirstAdditionalWordspaces' in given:
            d['speakFirstAdditionalWordspaces'] = js_float(given['speakFirstAdditionalWordspaces'], 0)

    # --- derived values -----------------------------------------------------

    def __getitem__(self, k):
        return self.d[k]

    @property
    def wpm(self):
        return self.true_wpm

    @property
    def fwpm(self):
        if self.d['syncWpm']:
            return self.true_wpm
        return self.true_fwpm if self.true_fwpm <= self.true_wpm else self.true_wpm

    @property
    def dit_frequency(self):
        return self.d['ditFrequency']

    @property
    def dah_frequency(self):
        return self.d['ditFrequency'] if self.d['syncFreq'] else self.d['dahFrequency']

    @property
    def override_min(self):
        return self.d['overrideSizeMin']

    @property
    def override_max(self):
        return self.d['overrideSizeMin'] if self.d['syncSize'] else self.d['overrideSizeMax']

    def multipliers(self):
        out = []
        for x in str(self.d['speedRacerMultipliers'] or '').split(','):
            n = js_float(x)
            if math.isfinite(n) and n > 0:
                out.append(n)
        return out

    def racer_total_plays(self):
        n = len(self.multipliers())
        if n <= 0:
            return 0
        return n + 1 if self.d['speedRacerFinalPlay'] else n

    def racer_active(self):
        return self.d['speedRacerEnabled'] and self.racer_total_plays() >= 1

    def applicable_speed(self, total_seconds):
        d = self.d
        if not d['speedInterval'] or not d['intervalTimingsText']:
            return self.wpm, self.fwpm
        times = [js_float(x) for x in d['intervalTimingsText'].split(',')]
        adj, run = [], 0.0
        for t in times:
            run += t
            adj.append(run)
        wpms = [js_int(x, None) for x in d['intervalWpmText'].split(',')]
        fwpms = [js_int(x, None) for x in d['intervalFwpmText'].split(',')]
        idx = -1
        for i, t in enumerate(adj):
            if idx == -1 and total_seconds < t:
                idx = i
        if idx == -1:
            idx = max(len(wpms) - 1, len(fwpms) - 1)
        w = wpms[idx] if len(wpms) - 1 >= idx else wpms[-1]
        f = fwpms[idx] if len(fwpms) - 1 >= idx else fwpms[-1]
        return (w if w is not None else self.wpm), (f if f is not None else self.fwpm)

    def apply_speed_racer(self, base, play_index):
        mults = self.multipliers()
        if not self.d['speedRacerEnabled'] or play_index < 0 or not mults:
            return base
        mult = mults[0] if play_index >= len(mults) else mults[play_index]
        vw = max(1, js_round(base[0] * mult))
        return vw, min(base[1], vw)

    def racer_pre_speak_pad_ms(self):
        mults = self.multipliers()
        if not mults:
            return 0
        base = max(1, js_round(self.wpm))
        last = max(1, js_round(base * mults[-1]))
        return max(350, js_round(60000 / (50 * last) * 7))

    def is_racer_speak_before_final_replay(self, play_index):
        if not self.d['speedRacerFinalPlay']:
            return False
        total = self.racer_total_plays()
        return total > 1 and play_index == total - 1

    def is_racer_speak_after_last_variation(self, play_index):
        if self.d['speedRacerFinalPlay']:
            return False
        mults = self.multipliers()
        return bool(mults) and play_index == len(mults) - 1

    def summary(self):
        d = self.d
        parts = [f'wpm={self.wpm}', f'fwpm={self.fwpm}', f'repeats={d["numberOfRepeats"]}',
                 f'cardWait={d["cardSpace"]}s', f'pre={d["preSpace"]}s',
                 f'voice={"on" if d["voiceEnabled"] else "off"}']
        if d['voiceEnabled']:
            mode = 'recap(armed)' if d['voiceRecap'] else ('first' if d['speakFirst'] else 'after')
            parts += [f'voiceMode={mode}', f'spell={d["voiceSpelling"]}',
                      f'delayBefore={d["voiceThinkingTime"]}s', f'delayAfter={d["voiceAfterThinkingTime"]}s']
        if d['speedRacerEnabled']:
            parts.append(f'speedRacer=[{d["speedRacerMultipliers"]}] replay={d["speedRacerFinalPlay"]} '
                         f'speak={d["speedRacerSpeakBeforeReplay"]}')
        if d['speedInterval']:
            parts.append(f'speedIntervals={d["intervalWpmText"]}/{d["intervalFwpmText"]}@{d["intervalTimingsText"]}')
        return ' '.join(parts)


# ---------------------------------------------------------------------------
# Catalog (wordlists.json + presets)
# ---------------------------------------------------------------------------

class Catalog:
    def __init__(self, repo):
        src = repo / 'src'
        if not (src / 'wordfilesconfigs' / 'wordlists.json').exists():
            sys.exit(f'error: {repo} does not look like the morsebrowser repo (use --repo)')
        self.src = src
        self.file_options = load_json(src / 'wordfilesconfigs' / 'wordlists.json')['fileOptions']
        self.class_presets = load_json(src / 'presets' / 'config.json')['classes']
        self.startup = [(s['key'], s['value']) for s in load_json(src / 'configs' / 'licwdefaults.json')['startupSettings']]
        self.legacy_mixin = [(s['key'], s['value']) for s in load_json(src / 'presets' / 'legacymixin' / 'legacymixin.json')['morseSettings']]
        self.overrides = load_json(src / 'presets' / 'overrides' / 'presetoverrides.json')['overrides']
        self.wordifier = Wordifier(load_json(src / 'configs' / 'wordify.json')['wordifications'])

    @staticmethod
    def _uniq(seq):
        out = []
        for x in seq:
            if x not in out:
                out.append(x)
        return out

    def types(self):
        return self._uniq(o['userTarget'] for o in self.file_options)

    def classes(self, utype):
        return self._uniq(o['class'] for o in self.file_options if o['userTarget'] == utype)

    def groups(self, utype, cls):
        return self._uniq(o['letterGroup'] for o in self.file_options if o['userTarget'] == utype and o['class'] == cls)

    def lessons(self, utype, cls, group):
        return [o for o in self.file_options
                if o['userTarget'] == utype and o['class'] == cls and o['letterGroup'] == group]

    def preset_options(self, cls, group):
        target = next((c for c in self.class_presets if c['className'] == cls), None)
        if not target:
            return []
        set_file = None
        for lg in target.get('letterGroups') or []:
            if lg.get('letterGroup') == group:
                set_file = lg.get('setFile')
        set_file = set_file or target.get('defaultSetFile')
        path = self.src / 'presets' / 'sets' / set_file if set_file else None
        if not path or not path.exists():
            return []
        return load_json(path).get('options', [])

    def preset_settings(self, filename):
        data = load_json(self.src / 'presets' / 'configs' / filename)
        return [(s['key'], s['value']) for s in data['morseSettings'] if s['key'] != 'showRaw']

    def overrides_for(self, group, file_name):
        out = []
        for o in self.overrides:
            f = o.get('filters', {})
            if group in f.get('letterGroup', []) or file_name in f.get('fileName', []):
                out += [(s['name'], s['value']) for s in o['settings']]
        return out

    def word_file(self, file_name):
        path = self.src / 'wordfiles' / file_name
        if file_name.endswith('txt'):
            with open(path, encoding='utf-8-sig', newline='') as f:
                return f.read()
        return load_json(path)


def pick(options, value, what):
    if value is None:
        return None
    for o in options:
        if o.upper() == value.upper():
            return o
    sys.exit(f'error: {what} "{value}" not found. Choices: {", ".join(options) or "(none)"}')


# ---------------------------------------------------------------------------
# Lesson text generation (randomWordList / shuffleWords)
# ---------------------------------------------------------------------------

def random_word_list(data, st, rng):
    stickys = ''
    if st['ifStickySets'] and st['stickySets'].strip():
        stickys = '|' + re.sub(' ', '|', st['stickySets'].upper().strip().replace('  ', ' '))
    chars = re.findall(f'<.*?>{stickys}|[^<.*?>]|\\W', str(data['letters']).upper())
    control_time = data.get('practiceSeconds')
    min_size = st.override_min if st['overrideSize'] else data.get('minWordSize', 1)
    max_size = st.override_max if st['overrideSize'] else data.get('maxWordSize', 1)
    min_size, max_size = js_int(min_size, 1), js_int(max_size, 1)

    def word_length(s):
        count, inside = 0, False
        for ch in s:
            if ch == '<':
                inside = True
                count += 1
            elif ch == '>':
                inside = False
            elif not inside:
                count += 1
        return count

    wpm, fwpm = st.applicable_speed(0)
    out, seconds = '', 0.0
    while True:
        word = ''
        n = min_size if min_size == max_size else rng.randint(min(min_size, max_size), max(min_size, max_size))
        for _ in range(n):
            if word_length(word) < n:
                free = n - word_length(word)
                usable = [x for x in chars if len(x) == 1 or (x.startswith('<') and x.endswith('>')) or len(x) <= free]
                if usable:
                    word += rng.choice(usable)
        out += (' ' + word.upper()) if seconds > 0 else word.upper()
        seconds = estimate_ms(out, wpm, fwpm, st['xtraWordSpaceDits']) / 1000
        # do { } while (seconds < controlTime); a missing practiceSeconds stops after one word.
        if control_time is None or seconds >= js_float(control_time, 0) or not chars:
            return out


def shuffle_text(raw_text, words, newline_chunking, intra_group, rng):
    has_phrases = '\n' in raw_text and newline_chunking
    groups, order, ungrouped = {}, [], []
    for w in words:
        gid = w.group_id()
        if gid is None:
            ungrouped.append([w])
        elif gid in groups:
            groups[gid].append(w)
        else:
            groups[gid] = [w]
            order.append(gid)
    units = []
    for gid in order:
        g = list(groups[gid])
        if intra_group:
            rng.shuffle(g)
        units.append(g)
    units += ungrouped
    rng.shuffle(units)
    return ('\n' if has_phrases else ' ').join(w.raw_word for u in units for w in u)


# ---------------------------------------------------------------------------
# Playback simulation (MorseViewModel.doPlay / playEnded / CardBufferManager)
# ---------------------------------------------------------------------------

class CardBuffer:
    def __init__(self):
        self.buffer = None  # list of subpart strings, or None when empty
        self.total_word_plays = 1
        self.last_audible_play_index = -1
        self.subparts_per_repeat = 1
        self.audible_play_count = 0

    def populate(self, word, repeats, additional_word_spaces):
        self.buffer = None
        if word is None:
            return
        subparts = [p for p in word.display_word.split(' ') if p]
        self.buffer = subparts
        self.subparts_per_repeat = max(1, len([p for p in subparts if p]))
        if repeats > 0:
            audible = list(subparts)
            self.buffer = []
            for r in range(repeats):
                self.buffer += audible
                if r < repeats - 1:
                    self.buffer += [''] * additional_word_spaces
            self.total_word_plays = repeats
        else:
            self.total_word_plays = 1
        self.last_audible_play_index = -1
        self.audible_play_count = 0

    def has_more(self):
        return bool(self.buffer)

    def repeat_state(self):
        pos = (self.audible_play_count - 1) % self.subparts_per_repeat if self.subparts_per_repeat > 0 else 0
        return {'index': self.last_audible_play_index, 'total': self.total_word_plays,
                'first': pos == 0, 'last': pos == self.subparts_per_repeat - 1}

    def next_morse(self, word, repeats, additional_word_spaces):
        if not self.has_more():
            self.populate(word, repeats, additional_word_spaces)
        if not self.has_more():
            return ''
        nxt = self.buffer.pop(0)
        if nxt:
            self.audible_play_count += 1
            self.last_audible_play_index = (self.audible_play_count - 1) // self.subparts_per_repeat
        return nxt

    def clear(self):
        self.buffer = None


class Session:
    """Replays the web app's scheduling and records tone/speech events (ms)."""

    def __init__(self, raw_text, words, st, speech_ms, arm_recap='end'):
        self.raw_text = raw_text
        self.words = words
        self.st = st
        self.speech_ms = speech_ms  # (text, spelled) -> duration ms
        self.arm_recap = arm_recap
        self.t = 0.0
        self.tones = []
        self.speech = []  # (start_ms, tts_text)
        self.cues = []    # (start_ms, kind, text)
        self.idx = 0
        self.cbm = CardBuffer()
        self.voice_buffer = []  # [(idx, txt)]
        self.speak_first_last = -1
        self.pre_space_used = False
        self.running_ms = 0.0
        self.last_partial = 0.0
        self.newline_chunking = st['keepLines']

    # --- helpers -----------------------------------------------------------

    def voice_on(self):
        return self.st['voiceEnabled']

    def speak_first(self):
        return self.st['speakFirst'] and self.voice_on()

    def spelled(self):
        return bool(self.st['voiceSpelling'])

    def auto_voice_allowed(self, racer_on):
        return not self.st['voiceRecap'] or racer_on

    def max_voice_buffer_reached(self):
        max_len = js_int(self.st['voiceBufferMaxLength'], 1)
        if max_len == 1:
            return True
        if not self.idx < len(self.words) - 1:
            return True
        return len(self.voice_buffer) == max_len

    def playing_seconds(self):
        ms = self.running_ms
        return math.floor(ms / 60000) * 60 + js_round((ms % 60000) / 1000)

    def say(self, phrase, spelled, label='VOICE'):
        text = tts_ready_text(phrase, spelled)
        dur = self.speech_ms(text) if text else 0.0
        if text:
            self.speech.append((self.t, text))
            self.cues.append((self.t, label, text))
        self.t += dur

    def delay_ms(self, seconds):
        v = js_float(seconds, 0)
        return v * 1000 if math.isfinite(v) and v > 0 else 0.0

    # --- view-model ports --------------------------------------------------

    def make_config(self, word, apply_racer, inter_repeat_dits):
        st = self.st
        w = do_replacements(word)
        wpm, fwpm = st.applicable_speed(self.playing_seconds())
        if apply_racer and st['speedRacerEnabled'] and w.strip():
            rs = self.cbm.repeat_state()
            if rs['total'] >= 1 and rs['index'] >= 0:
                wpm, fwpm = st.apply_speed_racer((wpm, fwpm), rs['index'])
        xtra = (js_int(st['xtraWordSpaceDits']) - 1) * 7
        if inter_repeat_dits > 0 and w.strip():
            xtra += inter_repeat_dits
        trim = False
        if self.auto_voice_allowed(st.racer_active()) and self.max_voice_buffer_reached():
            trim = self.voice_on() and not self.cbm.has_more()
        return {'word': w, 'wpm': js_int(wpm), 'fwpm': js_int(fwpm),
                'pre_ms': 0 if self.pre_space_used else js_float(st['preSpace'], 0) * 1000,
                'xtra': xtra, 'trim': trim}

    def add_to_voice_buffer(self):
        if self.st.racer_active():
            return
        last_idx = self.voice_buffer[-1][0] if self.voice_buffer else -1
        if self.idx > last_idx and self.idx >= len(self.voice_buffer):
            self.voice_buffer.append((self.idx, self.words[self.idx].speak_text(self.spelled())))

    def phrase_from_buffer(self):
        phrase = ' '.join(txt for _, txt in self.voice_buffer).replace('\n', ' ').strip()
        self.voice_buffer = []
        return phrase

    def render_play(self, cfg):
        tones, end = morse_play(cfg['word'], cfg['wpm'], cfg['fwpm'], self.st.dit_frequency, self.st.dah_frequency,
                                cfg['pre_ms'], cfg['xtra'], cfg['trim'])
        for start, dur, freq in tones:
            self.tones.append((self.t + start, dur, freq))
        if cfg['word'].strip():
            self.cues.append((self.t + cfg['pre_ms'], 'CW', f"{cfg['word'].strip()} @{cfg['wpm']}/{cfg['fwpm']}"))
        self.t += end

    def racer_recap(self):
        st = self.st
        text = self.words[self.idx].speak_text(self.spelled()).replace('\n', ' ').strip()
        if self.spelled():
            text = format_spelled_recap(text)
        self.t += self.delay_ms(st['voiceThinkingTime'])
        self.say(prep_phrase(text), self.spelled(), 'RECAP')
        self.t += self.delay_ms(st['voiceAfterThinkingTime'])

    def do_play(self, fresh):
        st = self.st
        if fresh:
            self.pre_space_used = False
            self.voice_buffer = []
            self.cbm.clear()
            self.speak_first_last = -1
        racer_on = st['speedRacerEnabled']
        racer_total = st.racer_total_plays() if racer_on else 0
        racer_active = racer_on and racer_total >= 1
        nrep = js_int(st['numberOfRepeats'])
        repeats = racer_total if racer_total >= 1 else (0 if nrep == 0 else nrep + 1)
        user_spacing = max(0.0, js_float(st['speakFirstAdditionalWordspaces'], 0) or 0)
        spacing = 1 if (racer_active and user_spacing == 0) else user_spacing
        whole = math.floor(spacing)
        frac_dits = (spacing - whole) * 7 if repeats > 0 else 0

        if not self.cbm.has_more():
            self.cues.append((self.t, 'CARD', f'#{self.idx + 1} {self.words[self.idx].display_word.strip()}'))
        word = self.cbm.next_morse(self.words[self.idx], repeats, whole)
        cfg = self.make_config(word, True, frac_dits)
        self.add_to_voice_buffer()
        rs = self.cbm.repeat_state()
        play_index = rs['index']
        audible = bool(cfg['word'].strip())
        speak_on = racer_active and st['speedRacerSpeakBeforeReplay'] and self.voice_on()
        self.last_partial = self.t
        self.pre_space_used = True

        if speak_on and audible and rs['first'] and st.is_racer_speak_before_final_replay(play_index):
            self.t += st.racer_pre_speak_pad_ms()
            self.racer_recap()
        elif racer_active or not self.speak_first() or self.speak_first_last == self.idx:
            pass
        else:
            phrase = self.phrase_from_buffer()
            cfg['pre_ms'] = 0
            self.t += self.delay_ms(st['voiceThinkingTime'])
            self.say(prep_phrase(phrase), self.spelled())
            self.t += self.delay_ms(st['voiceAfterThinkingTime'])
            self.speak_first_last = self.idx

        self.render_play(cfg)
        if speak_on and audible and rs['last'] and st.is_racer_speak_after_last_variation(play_index):
            self.t += st.racer_pre_speak_pad_ms()
            self.racer_recap()
        return self.play_ended(False)

    def play_ended(self, from_voice_or_trail):
        st = self.st
        is_not_last = self.idx < len(self.words) - 1
        any_newlines = '\n' in self.raw_text
        racer_on = st.racer_active()
        need_speak = (self.voice_on() and not from_voice_or_trail and not self.cbm.has_more()
                      and self.max_voice_buffer_reached() and not self.speak_first() and not racer_on)
        need_trail = to_bool(st['trailReveal']) and not racer_on and not from_voice_or_trail and not self.cbm.has_more()
        speak_and_trail = need_speak and need_trail

        if not need_speak and not need_trail:
            self.running_ms += self.t - self.last_partial
            if is_not_last or self.cbm.has_more():
                has_more = self.cbm.has_more()
                card_changed = False
                if not has_more:
                    if self.speak_first():
                        self.voice_buffer = []
                    self.idx += 1
                    card_changed = True
                if not card_changed and has_more:
                    delay = 0
                elif st['speedRacerEnabled']:
                    delay = max(self.delay_ms(st['cardSpace']), 800)
                else:
                    delay = self.delay_ms(st['cardSpace'])
                self.t += delay
                return 'next'
            if to_bool(st['trailReveal']):
                self.t += self.delay_ms(st['trailFinal'])
            return 'done'

        if need_speak:
            first_txt = self.voice_buffer[0][1] if self.voice_buffer else ''
            cond = self.auto_voice_allowed(racer_on) and (
                '\n' in first_txt or not is_not_last or not any_newlines or not self.newline_chunking)
            if cond and self.voice_buffer:
                phrase = self.phrase_from_buffer()
                if st['voiceLastOnly']:
                    phrase = phrase.split(' ')[-1]
                self.t += self.delay_ms(st['voiceThinkingTime'])
                self.say(prep_phrase(phrase), self.spelled())
                self.t += self.delay_ms(st['voiceAfterThinkingTime'])
            return self.play_ended(True)

        if need_trail and not speak_and_trail:
            self.t += self.delay_ms(st['trailPreDelay']) + self.delay_ms(st['trailPostDelay'])
            return self.play_ended(True)
        return 'done'

    def run(self):
        if not self.words:
            return
        result = self.do_play(True)
        while result == 'next':
            result = self.do_play(False)
        # Arm Recap: the site waits for the Voice Recap button, which speaks every
        # buffered card (speakVoiceBuffer). Export it after the Morse.
        st = self.st
        if self.voice_on() and st['voiceRecap'] and self.voice_buffer and self.arm_recap == 'end':
            self.t += 1000
            self.cues.append((self.t, 'RECAP', '--- voice recap ---'))
            for _, txt in self.voice_buffer:
                self.say(prep_phrase(txt), self.spelled(), 'RECAP')
                self.t += self.delay_ms(st['voiceAfterThinkingTime']) + 250


# ---------------------------------------------------------------------------
# TTS client
# ---------------------------------------------------------------------------

def parse_wav(data):
    """Return (samples: array('f'), sample_rate) from PCM16/PCM32/float32 WAV bytes."""
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('TTS response is not a WAV file')
    pos, fmt, raw = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack('<I', data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b'fmt ':
            fmt = struct.unpack('<HHIIHH', body[:16])
        elif cid == b'data':
            raw = body
        pos += 8 + size + (size & 1)
    if fmt is None or raw is None:
        raise ValueError('WAV missing fmt/data chunk')
    tag, channels, rate, _, _, bits = fmt
    if tag == 0xFFFE:
        tag = 3 if bits == 32 and len(raw) % 4 == 0 and _looks_float(raw) else 1
    if tag == 3 and bits == 32:
        samples = array.array('f')
        samples.frombytes(raw[:len(raw) - len(raw) % 4])
    elif tag == 1 and bits == 16:
        ints = array.array('h')
        ints.frombytes(raw[:len(raw) - len(raw) % 2])
        samples = array.array('f', (x / 32768.0 for x in ints))
    elif tag == 1 and bits == 32:
        ints = array.array('i')
        ints.frombytes(raw[:len(raw) - len(raw) % 4])
        samples = array.array('f', (x / 2147483648.0 for x in ints))
    else:
        raise ValueError(f'unsupported WAV format tag={tag} bits={bits}')
    if sys.byteorder == 'big':
        samples.byteswap()
    if channels > 1:
        samples = array.array('f', (sum(samples[i:i + channels]) / channels
                                    for i in range(0, len(samples), channels)))
    return samples, rate


def _looks_float(raw):
    probe = array.array('f')
    probe.frombytes(raw[:4000 - 4000 % 4])
    return all(math.isfinite(x) and abs(x) <= 4 for x in probe)


def resample(samples, src_rate, dst_rate):
    if src_rate == dst_rate or not samples:
        return samples
    n = int(len(samples) * dst_rate / src_rate)
    step = src_rate / dst_rate
    last = len(samples) - 1
    out = array.array('f', bytes(4 * n))
    for i in range(n):
        x = i * step
        j = int(x)
        frac = x - j
        a = samples[j] if j <= last else 0.0
        b = samples[j + 1] if j + 1 <= last else a
        out[i] = a + (b - a) * frac
    return out


class TTSClient:
    def __init__(self, base_url, voice, speed, language, instruct, cache_dir, concurrency,
                 sample_rate, peak, trim):
        self.base_url = base_url.rstrip('/')
        self.voice = voice
        self.speed = speed
        self.language = language
        self.instruct = instruct
        self.cache_dir = Path(cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.concurrency = max(1, concurrency)
        self.sample_rate = sample_rate
        self.peak = peak
        self.trim = trim
        self._clips = {}

    def check(self):
        try:
            with urllib.request.urlopen(self.base_url + '/health', timeout=10) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def _cache_path(self, text):
        key = json.dumps([self.voice, self.speed, self.language, self.instruct, text], ensure_ascii=False)
        return self.cache_dir / (hashlib.sha1(key.encode('utf-8')).hexdigest() + '.wav')

    def _fetch(self, text):
        path = self._cache_path(text)
        if path.exists() and path.stat().st_size > 44:
            return path.read_bytes()
        body = {'input': text, 'voice': self.voice, 'response_format': 'wav', 'speed': self.speed,
                'language': self.language}
        if self.instruct:
            body['instruct'] = self.instruct
        req = urllib.request.Request(self.base_url + '/v1/audio/speech', data=json.dumps(body).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
        err = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = r.read()
                parse_wav(data)
                tmp = path.with_suffix('.tmp')
                tmp.write_bytes(data)
                tmp.replace(path)
                return data
            except (urllib.error.URLError, OSError, ValueError) as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f'TTS failed for {text!r}: {err}')

    def prefetch(self, texts):
        todo = [t for t in dict.fromkeys(texts) if t and not self._cache_path(t).exists()]
        if not todo:
            return
        done = 0
        with concurrent.futures.ThreadPoolExecutor(self.concurrency) as pool:
            for fut in concurrent.futures.as_completed([pool.submit(self._fetch, t) for t in todo]):
                fut.result()
                done += 1
                print(f'\r  TTS {done}/{len(todo)}', end='', file=sys.stderr, flush=True)
        print(file=sys.stderr)

    def clip(self, text):
        if text in self._clips:
            return self._clips[text]
        samples, rate = parse_wav(self._fetch(text))
        samples = resample(samples, rate, self.sample_rate)
        peak = max((abs(x) for x in samples), default=0.0)
        if self.trim and peak > 0:
            thr = peak * 0.02
            first = next((i for i, x in enumerate(samples) if abs(x) > thr), 0)
            last = next((i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) > thr), len(samples) - 1)
            pad = int(0.03 * self.sample_rate)
            samples = samples[max(0, first - pad):min(len(samples), last + pad + 1)]
        if peak > 0:
            g = self.peak / peak
            samples = array.array('f', (x * g for x in samples))
        self._clips[text] = samples
        return samples

    def duration_ms(self, text):
        return len(self.clip(text)) * 1000.0 / self.sample_rate


# ---------------------------------------------------------------------------
# Audio rendering
# ---------------------------------------------------------------------------

class ToneBank:
    """Gated sine with the live player's exponential attack/decay (setTargetAtTime)."""

    def __init__(self, sample_rate, amplitude, rise_tc, decay_tc, decay_offset_ms):
        self.sr = sample_rate
        self.amp = amplitude
        self.rise = max(1e-5, rise_tc)
        self.decay = max(1e-5, decay_tc)
        self.decay_offset = decay_offset_ms / 1000.0
        self.cache = {}

    def segment(self, dur_ms, freq):
        n_on = int(round(dur_ms * self.sr / 1000))
        key = (n_on, freq)
        if key in self.cache:
            return self.cache[key]
        dur = n_on / self.sr
        release_at = max(0.0, dur - self.decay_offset)
        n = n_on + int(math.ceil(5 * self.decay * self.sr))
        w = 2 * math.pi * freq / self.sr
        level_at_release = 1 - math.exp(-release_at / self.rise)
        seg = array.array('f', bytes(4 * n))
        for i in range(n):
            t = i / self.sr
            if t < release_at:
                env = 1 - math.exp(-t / self.rise)
            else:
                env = level_at_release * math.exp(-(t - release_at) / self.decay)
            seg[i] = self.amp * env * math.sin(w * i)
        self.cache[key] = seg
        return seg


def mix_into(out, start, clip):
    if start >= len(out) or not clip:
        return
    end = min(len(out), start + len(clip))
    out[start:end] = array.array('f', map(operator.add, out[start:end], clip[:end - start]))


def render(session, tts, st, sample_rate, tone_peak, tail_ms=500):
    total = int((session.t + tail_ms) * sample_rate / 1000) + 1
    out = array.array('f', bytes(4 * total))
    bank = ToneBank(sample_rate, tone_peak * js_float(st['volume'], 10) / 10,
                    js_float(st['riseTimeConstant'], 0.001), js_float(st['decayTimeConstant'], 0.001),
                    js_float(st['decayMsOffset'], 1.5))
    for start, dur, freq in session.tones:
        mix_into(out, int(round(start * sample_rate / 1000)), bank.segment(dur, freq))
    vgain = js_float(st['voiceVolume'], 10) / 10
    for start, text in session.speech:
        clip = tts.clip(text)
        if vgain != 1:
            clip = array.array('f', (x * vgain for x in clip))
        mix_into(out, int(round(start * sample_rate / 1000)), clip)
    return out


def write_float_wav(path, samples, sample_rate):
    data = samples.tobytes() if sys.byteorder == 'little' else _swapped(samples)
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 36 + len(data)) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 3, 1, sample_rate, sample_rate * 4, 4, 32))
        f.write(b'data' + struct.pack('<I', len(data)))
        f.write(data)


def _swapped(samples):
    s = array.array('f', samples)
    s.byteswap()
    return s.tobytes()


def write_output(path, samples, sample_rate, fmt, title, bitrate):
    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        if fmt == 'mp3':
            sys.exit('error: ffmpeg is required for MP3 output (or use --format wav)')
        ints = array.array('h', (int(max(-1.0, min(1.0, x)) * 32767) for x in samples))
        import wave
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(ints.tobytes())
        return
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'mix.wav'
        write_float_wav(src, samples, sample_rate)
        cmd = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(src), '-ac', '1',
               '-metadata', f'title={title}', '-metadata', 'artist=LICW Morse Practice']
        cmd += ['-codec:a', 'libmp3lame', '-b:a', bitrate] if fmt == 'mp3' else ['-codec:a', 'pcm_s16le']
        subprocess.run(cmd + [str(path)], check=True)


def write_cues(path, session):
    def ts(ms):
        s = ms / 1000
        return f'{int(s // 60):02d}:{s % 60:06.3f}'
    with open(path, 'w', encoding='utf-8') as f:
        for t, kind, text in session.cues:
            f.write(f'{ts(t)}  {kind:<5} {text}\n')
        f.write(f'{ts(session.t)}  END\n')


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, name, settings, raw_text_fn, newline_chunking, label):
        self.name = name
        self.settings = settings
        self.raw_text_fn = raw_text_fn  # rng -> text
        self.newline_chunking = newline_chunking
        self.label = label


def build_settings(cat, preset_pairs, group, file_name, user_sets):
    st = Settings()
    st.apply(cat.startup)
    if preset_pairs is not None:
        pairs = list(preset_pairs)
        keys = {k for k, _ in pairs}
        pairs += [(k, v) for k, v in cat.legacy_mixin if k not in keys]
        for k, v in cat.overrides_for(group, file_name):
            pairs = [(pk, v if pk == k else pv) for pk, pv in pairs]
            if k not in {pk for pk, _ in pairs}:
                pairs.append((k, v))
        st.apply(pairs, PRESET_KEY_BLACKLIST)
    if user_sets:
        st.apply(user_sets)
    return st


def parse_sets(items):
    out = []
    for item in items or []:
        if '=' not in item:
            sys.exit(f'error: --set expects key=value, got {item!r}')
        k, v = item.split('=', 1)
        v = v.strip()
        out.append((k.strip(), True if v.lower() == 'true' else False if v.lower() == 'false' else v))
    return out


def slug(*parts):
    s = '_'.join(p for p in parts if p)
    s = re.sub(r'[^A-Za-z0-9._-]+', '-', s).strip('-_')
    return re.sub(r'-{2,}', '-', s) or 'morse'


def plan_jobs(cat, args):
    user_sets = parse_sets(args.set)
    if args.no_voice:
        user_sets.append(('voiceEnabled', False))

    if args.text_file or args.text is not None:
        text = Path(args.text_file).read_text(encoding='utf-8-sig') if args.text_file else args.text
        pairs = None
        label = YOUR_SETTINGS
        if args.preset_file:
            data = load_json(args.preset_file)
            pairs = [(s['key'], s['value']) for s in data['morseSettings'] if s['key'] != 'showRaw']
            label = Path(args.preset_file).stem
        st = build_settings(cat, pairs, None, None, user_sets)
        name = slug(Path(args.text_file).stem if args.text_file else 'text', label)
        return [Job(name, st, lambda rng: text, st['keepLines'], f'{name}')]

    utype = pick(cat.types(), args.type or 'STUDENT', 'TYPE')
    if not args.cls or not args.group or not args.lesson:
        sys.exit('error: render needs --class, --group and --lesson (or --link / --text-file). Try "list".')
    cls = pick(cat.classes(utype), args.cls, 'CLASS')
    group = pick(cat.groups(utype, cls), args.group, 'CONTENT')
    lessons = cat.lessons(utype, cls, group)
    if args.lesson.lower() != 'all':
        displays = [l['display'] for l in lessons]
        chosen = pick(displays, args.lesson, 'LESSON')
        lessons = [next(l for l in lessons if l['display'] == chosen)]

    options = cat.preset_options(cls, group)
    if args.preset_file:
        data = load_json(args.preset_file)
        presets = [(Path(args.preset_file).stem,
                    [(s['key'], s['value']) for s in data['morseSettings'] if s['key'] != 'showRaw'])]
    elif args.preset and args.preset.lower() == 'all':
        presets = [(o['display'], o['filename']) for o in options] or [(YOUR_SETTINGS, None)]
    elif args.preset:
        if args.preset.upper() == YOUR_SETTINGS.upper():
            presets = [(YOUR_SETTINGS, None)]
        else:
            chosen = pick([o['display'] for o in options], args.preset, 'PRESET')
            presets = [next((o['display'], o['filename']) for o in options if o['display'] == chosen)]
    else:
        presets = [(options[0]['display'], options[0]['filename'])] if options else [(YOUR_SETTINGS, None)]

    jobs = []
    for lesson in lessons:
        for pdisplay, pref in presets:
            if pref is None:
                pairs = []  # "Your Settings": site defaults + legacy mixin + overrides
            elif isinstance(pref, list):
                pairs = pref
            else:
                pairs = cat.preset_settings(pref)
            st = build_settings(cat, pairs, group, lesson['fileName'], user_sets)

            def text_fn(rng, lesson=lesson, st=st):
                data = cat.word_file(lesson['fileName'])
                return data if isinstance(data, str) else random_word_list(data, st, rng)
            name = slug(cls, group, lesson['display'], pdisplay)
            label = f'{cls} / {group} / {lesson["display"]} / {pdisplay}'
            jobs.append(Job(name, st, text_fn, bool(lesson.get('newlineChunking')), label))
    return jobs


def run_job(job, cat, args, tts, rng):
    st = job.settings
    st.d['keepLines'] = job.newline_chunking
    raw = job.raw_text_fn(rng)
    words = get_words(raw, job.newline_chunking, cat.wordifier)
    if st['isShuffledSet']:
        raw = shuffle_text(raw, words, job.newline_chunking, st['shuffleIntraGroup'], rng)
        words = get_words(raw, job.newline_chunking, cat.wordifier)
    if not words:
        print(f'  skipped: no cards in {job.label}', file=sys.stderr)
        return None

    # Pass 1 collects phrases (durations don't change what is said), pass 2 uses real durations.
    probe = Session(raw, words, st, lambda text: 1000.0, args.arm_recap)
    probe.run()
    phrases = [text for _, text in probe.speech]
    if phrases and tts is None and not args.dry_run:
        sys.exit('error: this preset uses voice; set --tts-url or pass --no-voice')
    if phrases and not args.dry_run:
        tts.prefetch(phrases)

    duration = (lambda text: 600.0 + 350.0 * len(text.split())) if args.dry_run else tts.duration_ms
    session = Session(raw, words, st, duration, args.arm_recap)
    session.run()
    return session


def cmd_render(cat, args):
    if args.link:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(args.link).query)
        get = lambda k: q.get(k, [None])[0]
        args.type = args.type or get('selectedType')
        args.cls = args.cls or get('selectedClass')
        args.group = args.group or get('selectedGroup')
        args.lesson = args.lesson or get('selectedLesson')
        args.preset = args.preset or get('selectedPreset')

    jobs = plan_jobs(cat, args)
    if args.output and len(jobs) > 1:
        sys.exit('error: -o/--output names one file; use --out-dir when rendering several')
    tts = None
    if args.tts_url:
        tts = TTSClient(args.tts_url, args.tts_voice, args.tts_speed, args.tts_language, args.tts_instruct,
                        args.cache_dir, args.tts_concurrency, args.sample_rate,
                        10 ** (args.voice_db / 20), not args.no_trim)
        if not args.dry_run and not tts.check():
            print(f'warning: TTS health check failed at {args.tts_url}', file=sys.stderr)

    seed = args.seed if args.seed is not None else random.randrange(1 << 30)
    for n, job in enumerate(jobs, 1):
        print(f'[{n}/{len(jobs)}] {job.label}', file=sys.stderr)
        print(f'  {job.settings.summary()}', file=sys.stderr)
        rng = random.Random(seed + n)
        session = run_job(job, cat, args, tts, rng)
        if session is None:
            continue
        out = Path(args.output) if args.output else Path(args.out_dir) / f'{job.name}.{args.format}'
        print(f'  {len(session.words)} cards, {len(session.speech)} spoken phrases, '
              f'{session.t / 60000:.1f} min', file=sys.stderr)
        if args.dry_run:
            for t, kind, text in session.cues[:args.dry_run_lines]:
                print(f'    {t / 1000:8.2f}s  {kind:<5} {text}')
            continue
        samples = render(session, tts, job.settings, args.sample_rate, 10 ** (args.tone_db / 20))
        write_output(out, samples, args.sample_rate, args.format, job.label, args.bitrate)
        if args.cues:
            write_cues(out.with_suffix('.cues.txt'), session)
        print(f'  -> {out}', file=sys.stderr)


def cmd_list(cat, args):
    utype = pick(cat.types(), args.type or 'STUDENT', 'TYPE')
    if not args.cls:
        print(f'TYPE: {", ".join(cat.types())}  (showing {utype})')
        for c in cat.classes(utype):
            print(f'  CLASS {c}: {len(cat.groups(utype, c))} content groups')
        return
    cls = pick(cat.classes(utype), args.cls, 'CLASS')
    if not args.group:
        for g in cat.groups(utype, cls):
            print(f'  CONTENT {g}: {len(cat.lessons(utype, cls, g))} lessons')
        return
    group = pick(cat.groups(utype, cls), args.group, 'CONTENT')
    print(f'{utype} / {cls} / {group}')
    print('  LESSONS:')
    for l in cat.lessons(utype, cls, group):
        print(f'    {l["display"]}   [{l["fileName"]}]')
    print('  PRESETS:')
    options = cat.preset_options(cls, group)
    for o in options:
        print(f'    {o["display"]}   [{o["filename"]}]')
    if not options:
        print(f'    {YOUR_SETTINGS} (no preset set for this class)')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0].strip(),
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__[__doc__.index('Examples'):])
    p.add_argument('--repo', default=str(DEFAULT_REPO), help='morsebrowser checkout (default: this repo)')
    sub = p.add_subparsers(dest='cmd', required=True)

    def selection(sp):
        sp.add_argument('--type', help='TYPE (default STUDENT)')
        sp.add_argument('--class', dest='cls', help='CLASS, e.g. BC1')
        sp.add_argument('--group', help='CONTENT, e.g. REA')

    lp = sub.add_parser('list', help='browse classes, content groups, lessons and presets')
    selection(lp)

    rp = sub.add_parser('render', help='render lesson audio')
    selection(rp)
    rp.add_argument('--lesson', help='LESSON display name, or "all"')
    rp.add_argument('--preset', help='PRESETS display name, "Your Settings", or "all" (default: first preset)')
    rp.add_argument('--link', help='site deep link with selectedClass/selectedGroup/selectedLesson/selectedPreset')
    rp.add_argument('--text', help='practice text instead of a lesson')
    rp.add_argument('--text-file', help='practice text file instead of a lesson')
    rp.add_argument('--preset-file', help='settings JSON (e.g. LICWSettings.json saved from the site)')
    rp.add_argument('--set', action='append', metavar='KEY=VALUE',
                    help='override a setting by preset key (repeatable), e.g. wpm=20 voiceEnabled=false')
    rp.add_argument('--no-voice', action='store_true', help='Morse only')
    rp.add_argument('--arm-recap', choices=['end', 'none'], default='end',
                    help='Arm Recap presets: speak the recap after the Morse (default) or skip it')
    rp.add_argument('-o', '--output', help='output file (single render)')
    rp.add_argument('--out-dir', default='morse_audio', help='output folder (default ./morse_audio)')
    rp.add_argument('--format', choices=['mp3', 'wav'], default='mp3')
    rp.add_argument('--bitrate', default='96k', help='MP3 bitrate (default 96k)')
    rp.add_argument('--sample-rate', type=int, default=24000)
    rp.add_argument('--tone-db', type=float, default=-9.0, help='tone peak at volume 10, dBFS (default -9)')
    rp.add_argument('--voice-db', type=float, default=-2.0, help='speech peak, dBFS (default -2)')
    rp.add_argument('--seed', type=int, help='random seed for generated groups / shuffle')
    rp.add_argument('--cues', action='store_true', help='also write a .cues.txt timeline next to the audio')
    rp.add_argument('--dry-run', action='store_true', help='print the timeline only (no TTS, no audio)')
    rp.add_argument('--dry-run-lines', type=int, default=40)
    rp.add_argument('--tts-url', default=DEFAULT_TTS_URL, help=f'TTS base URL (default {DEFAULT_TTS_URL})')
    rp.add_argument('--tts-voice', default=DEFAULT_TTS_VOICE, help='TTS voice (default Aiden)')
    rp.add_argument('--tts-speed', type=float, default=1.0)
    rp.add_argument('--tts-language', default='English')
    rp.add_argument('--tts-instruct', default='', help='optional style instruction for the TTS model')
    rp.add_argument('--tts-concurrency', type=int, default=3)
    rp.add_argument('--no-trim', action='store_true', help='keep leading/trailing silence from TTS clips')
    rp.add_argument('--cache-dir', default='~/.cache/morse_audio_export/tts')

    args = p.parse_args(argv)
    cat = Catalog(Path(args.repo))
    if args.cmd == 'list':
        cmd_list(cat, args)
    else:
        cmd_render(cat, args)


if __name__ == '__main__':
    main()
