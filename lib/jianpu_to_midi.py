import re
import mido
import os
import glob
from mido import Message, MidiFile, MidiTrack


KEY_OFFSETS = {
    'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3,
    'E': 4, 'E#': 5, 'Fb': 4, 'F': 5, 'F#': 6, 'Gb': 6,
    'G': 7, 'G#': 8, 'Ab': 8, 'A': 9, 'A#': 10, 'Bb': 10, 'B': 11
}


ALPHABET_TO_NOTE = {
    'Q': '+1', 'W': '+2', 'E': '+3', 'R': '+4', 'T': '+5', 'Y': '+6', 'U': '+7',
    'A': '1', 'S': '2', 'D': '3', 'F': '4', 'G': '5', 'H': '6', 'J': '7',
    'Z': '-1', 'X': '-2', 'C': '-3', 'V': '-4', 'B': '-5', 'N': '-6', 'M': '-7'
}


def parse_metadata(first_line):
    key = 'C'
    tempo = 120
    time_signature = (4, 4)
    
    match = re.search(r'([CDEFGAB][#b]?)大调', first_line)
    if match:
        key = match.group(1)
    
    match = re.search(r'(\d+)\s*BPM', first_line)
    if match:
        tempo = int(match.group(1))
    
    match = re.search(r'(\d+)/(\d+)', first_line)
    if match:
        time_signature = (int(match.group(1)), int(match.group(2)))
    
    return key, tempo, time_signature


class JianpuParser:
    def __init__(self, key='C'):
        self.scale_degrees = {'1': 0, '2': 2, '3': 4, '4': 5, '5': 7, '6': 9, '7': 11}
        self.default_octave = 0
        self.current_octave = self.default_octave
        self.key_offset = KEY_OFFSETS.get(key, 0)

    def parse_octave_modifier(self, token):
        octave_mod = 0
        remaining = token
        for c in token:
            if c == '+':
                octave_mod += 1
                remaining = remaining[1:]
            elif c == '-':
                octave_mod -= 1
                remaining = remaining[1:]
            else:
                break
        return octave_mod, remaining

    def parse_note(self, token):
        if token in ALPHABET_TO_NOTE:
            token = ALPHABET_TO_NOTE[token]
        
        octave_mod, note_str = self.parse_octave_modifier(token)
        if not note_str:
            return None
        
        note_num = note_str[0]
        if note_num not in self.scale_degrees:
            return None
        
        octave = self.default_octave + octave_mod
        if octave < 0:
            octave = 0
        if octave > 8:
            octave = 8
        
        midi_note = 12 * octave + 60 + self.scale_degrees[note_num] + self.key_offset
        if midi_note < 0:
            midi_note = 0
        if midi_note > 127:
            midi_note = 127
        return midi_note

    def parse_chord(self, chord_str):
        notes = []
        chord_str = chord_str.strip('()')
        
        parts = []
        i = 0
        while i < len(chord_str):
            if chord_str[i] in '+-':
                octave_mods = ''
                j = i
                while j < len(chord_str) and chord_str[j] in '+-':
                    octave_mods += chord_str[j]
                    j += 1
                if j < len(chord_str) and chord_str[j] in '1234567':
                    parts.append(octave_mods + chord_str[j])
                    i = j + 1
                else:
                    i = j
            elif chord_str[i] in '1234567':
                parts.append(chord_str[i])
                i += 1
            elif chord_str[i] in ALPHABET_TO_NOTE:
                parts.append(chord_str[i])
                i += 1
            else:
                i += 1
        
        for part in parts:
            note = self.parse_note(part)
            if note is not None:
                notes.append(note)
        return notes

    def tokenize(self, text):
        tokens = []
        lines = text.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.endswith('：'):
                if line.endswith('：'):
                    tokens.append(('section', line[:-1]))
                continue
            
            if line == '(:)':
                tokens.append(('repeat_start', None))
                continue
            
            if '反复' in line or '结尾' in line:
                if '从(:)处反复' in line:
                    tokens.append(('repeat_to_start', None))
                continue
            
            i = 0
            while i < len(line):
                if line[i] == '(':
                    j = line.find(')', i)
                    if j != -1:
                        chord_str = line[i:j+1]
                        tokens.append(('chord', chord_str))
                        i = j + 1
                        continue
                elif line[i] == '/':
                    tokens.append(('bar', None))
                    i += 1
                    continue
                elif line[i] in '+-':
                    octave_modifiers = ''
                    j = i
                    while j < len(line) and line[j] in '+-':
                        octave_modifiers += line[j]
                        j += 1
                    if j < len(line) and line[j] in '1234567':
                        tokens.append(('note', octave_modifiers + line[j]))
                        i = j + 1
                    else:
                        i = j
                    continue
                elif line[i] in '1234567':
                    tokens.append(('note', line[i]))
                    i += 1
                    continue
                elif line[i] in ALPHABET_TO_NOTE:
                    tokens.append(('note', line[i]))
                    i += 1
                    continue
                else:
                    i += 1
        
        return tokens


class MidiGenerator:
    def __init__(self, tempo=120, time_signature=(4, 4), key='C'):
        self.tempo = tempo
        self.time_signature = time_signature
        self.ticks_per_beat = 480
        # 根据拍号分母计算基本音符时值
        # 4/4拍: 分母4 → 四分音符 = 480 ticks
        # 6/8拍: 分母8 → 八分音符 = 240 ticks
        self.beat_unit = self.ticks_per_beat * 4 // time_signature[1]
        self.default_duration = self.beat_unit
        self.channel = 0
        self.velocity = 64
        self.key = key

    def generate_track(self, tokens):
        track = MidiTrack()
        track.append(mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(self.tempo)))
        track.append(mido.MetaMessage('time_signature', 
                                      numerator=self.time_signature[0],
                                      denominator=self.time_signature[1]))
        track.append(mido.MetaMessage('key_signature', key=self.key))
        track.append(Message('program_change', program=0, time=0))
        
        pending_delta = 0
        parser = JianpuParser(key=self.key)
        
        for token_type, token_value in tokens:
            if token_type == 'repeat_start':
                continue
            elif token_type == 'repeat_to_start':
                continue
            elif token_type == 'section':
                continue
            elif token_type == 'bar':
                bar_duration = self.time_signature[0] * self.beat_unit
                if pending_delta % bar_duration != 0:
                    pending_delta = ((pending_delta // bar_duration) + 1) * bar_duration
                continue
            
            if token_type == 'note':
                midi_note = parser.parse_note(token_value)
                if midi_note is not None:
                    track.append(Message('note_on', channel=self.channel,
                                         note=midi_note, velocity=self.velocity,
                                         time=pending_delta))
                    track.append(Message('note_off', channel=self.channel,
                                         note=midi_note, velocity=0,
                                         time=self.default_duration))
                    pending_delta = 0
            elif token_type == 'chord':
                notes = parser.parse_chord(token_value)
                if notes:
                    for i, note in enumerate(notes):
                        delta = pending_delta if i == 0 else 0
                        track.append(Message('note_on', channel=self.channel,
                                             note=note, velocity=self.velocity,
                                             time=delta))
                    for i, note in enumerate(notes):
                        delta = self.default_duration if i == 0 else 0
                        track.append(Message('note_off', channel=self.channel,
                                             note=note, velocity=0,
                                             time=delta))
                    pending_delta = 0
        
        track.append(mido.MetaMessage('end_of_track', time=pending_delta))
        return track, None


def jianpu_to_midi(input_path, output_path, tempo=120, key='C', time_signature=(4, 4)):
    with open(input_path, 'r', encoding='utf-8') as f:
        text = f.read()
    
    lines = text.split('\n')
    if lines:
        key, tempo, time_signature = parse_metadata(lines[0])
        text = '\n'.join(lines[1:])
    
    parser = JianpuParser(key=key)
    tokens = parser.tokenize(text)
    
    mid = MidiFile()
    generator = MidiGenerator(tempo=tempo, time_signature=time_signature, key=key)
    track, repeat_start = generator.generate_track(tokens)
    
    mid.tracks.append(track)
    mid.save(output_path)
    return len(tokens), repeat_start is not None, key, tempo, time_signature


def batch_convert(source_dir, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    
    txt_files = glob.glob(os.path.join(source_dir, '*.txt'))
    if not txt_files:
        print(f"No txt files found in {source_dir}")
        return
    
    print(f"Found {len(txt_files)} txt files to convert:\n")
    
    for txt_path in txt_files:
        file_name = os.path.basename(txt_path)
        base_name = os.path.splitext(file_name)[0]
        mid_path = os.path.join(output_dir, f"{base_name}.mid")
        
        try:
            token_count, has_repeat, key, tempo, time_sig = jianpu_to_midi(txt_path, mid_path)
            print(f"[OK] {file_name}")
            print(f"  -> {base_name}.mid")
            print(f"    Key: {key}, Tempo: {tempo} BPM, Time: {time_sig[0]}/{time_sig[1]}")
            print(f"    Tokens: {token_count}, Notes: {token_count // 2}\n")
        except Exception as e:
            print(f"[FAIL] {file_name}")
            print(f"    Error: {str(e)}\n")


if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    source_dir = os.path.join(project_root, 'source')
    output_dir = os.path.join(project_root, 'output')
    
    batch_convert(source_dir, output_dir)