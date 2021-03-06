# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Defines sequence of notes objects for creating datasets."""

import collections
import copy

# internal imports

from magenta.music import constants
from magenta.protobuf import music_pb2

# Set the quantization cutoff.
# Note events before this cutoff are rounded down to nearest step. Notes
# above this cutoff are rounded up to nearest step. The cutoff is given as a
# fraction of a step.
# For example, with quantize_cutoff = 0.75 using 0-based indexing,
# if .75 < event <= 1.75, it will be quantized to step 1.
# If 1.75 < event <= 2.75 it will be quantized to step 2.
# A number close to 1.0 gives less wiggle room for notes that start early,
# and they will be snapped to the previous step.
QUANTIZE_CUTOFF = 0.5

# Shortcut to chord symbol text annotation type.
CHORD_SYMBOL = music_pb2.NoteSequence.TextAnnotation.CHORD_SYMBOL


class BadTimeSignatureException(Exception):
  pass


class MultipleTimeSignatureException(Exception):
  pass


class MultipleTempoException(Exception):
  pass


class NegativeTimeException(Exception):
  pass


def extract_subsequence(sequence, start_time, end_time):
  """Extracts a subsequence from a NoteSequence.

  Notes starting before `start_time` are not included. Notes ending after
  `end_time` are truncated.

  Args:
    sequence: The NoteSequence to extract a subsequence from.
    start_time: The float time in seconds to start the subsequence.
    end_time: The float time in seconds to end the subsequence.

  Returns:
    A new NoteSequence that is a subsequence of `sequence` in the specified time
    range.
  """
  subsequence = music_pb2.NoteSequence()
  subsequence.CopyFrom(sequence)
  del subsequence.notes[:]
  for note in sequence.notes:
    if note.start_time < start_time or note.start_time >= end_time:
      continue
    new_note = subsequence.notes.add()
    new_note.CopyFrom(note)
    new_note.end_time = min(note.end_time, end_time)
  subsequence.total_time = min(sequence.total_time, end_time)
  return subsequence


def is_power_of_2(x):
  return x and not x & (x - 1)


class QuantizedSequence(object):
  """Holds notes and chords which have been quantized to time steps.

  Notes contain a pitch, velocity, start time, and end time. Notes
  are stored in tracks (which can be different instruments or the same
  instrument). There is also a time signature and key signature.

  Notes stored in this object are not guaranteed to be sorted by time.

  Attributes:
    tracks: A dictionary mapping track number to list of Note tuples. Track
        number is taken from the instrument number of each NoteSequence note.
    chords: A list of ChordSymbol tuples.
    qpm: Quarters per minute. This is needed to recover tempo if converting back
        to MIDI.
    time_signature: This determines the length of a bar of music. This is just
        needed to compute the number of quantization steps per bar, though it
        can also communicate more high level aspects of the music
        (see https://en.wikipedia.org/wiki/Time_signature).
    steps_per_quarter: How many quantization steps per quarter note of music.
    total_steps: The total number of steps in the quantized sequence.
  """

  # Disabling pylint since it is recognizing these as attributes instead of
  # classes.
  # pylint: disable=invalid-name
  Note = collections.namedtuple(
      'Note',
      ['pitch', 'velocity', 'start', 'end', 'instrument', 'program', 'is_drum'])
  TimeSignature = collections.namedtuple('TimeSignature',
                                         ['numerator', 'denominator'])
  ChordSymbol = collections.namedtuple('ChordSymbol', ['step', 'figure'])
  # pylint: enable=invalid-name

  def __init__(self):
    self._reset()

  def _reset(self):
    self.tracks = {}
    self.chords = []
    self.qpm = 120.0
    self.time_signature = QuantizedSequence.TimeSignature(numerator=4,
                                                          denominator=4)
    self.steps_per_quarter = 4
    self.total_steps = 0

  def steps_per_bar(self):
    """Calculates steps per bar.

    Returns:
      Steps per bar as a floating point number.
    """
    quarters_per_beat = 4.0 / self.time_signature.denominator
    quarters_per_bar = (quarters_per_beat * self.time_signature.numerator)
    steps_per_bar_float = (self.steps_per_quarter * quarters_per_bar)
    return steps_per_bar_float

  def from_note_sequence(self, note_sequence, steps_per_quarter):
    """Populate self with a music_pb2.NoteSequence proto.

    Notes and time signature are saved to self with notes' start and end times
    quantized. If there is no time signature 4/4 is assumed. If there is more
    than one time signature an exception is raised.

    The quarter notes per minute stored in `note_sequence` is used to normalize
    tempo. Regardless of how fast or slow quarter notes are played, a note that
    is played for 1 quarter note will last `steps_per_quarter` time steps in
    the quantized result.

    A note's start and end time are snapped to a nearby quantized step. See
    the comments above `QUANTIZE_CUTOFF` for details.

    Args:
      note_sequence: A music_pb2.NoteSequence protocol buffer.
      steps_per_quarter: Each quarter note of music will be divided into this
          many quantized time steps.

    Raises:
      MultipleTimeSignatureException: If there is a change in time signature
          in `note_sequence`.
      MultipleTempoException: If there is a change in tempo in `note_sequence`.
      BadTimeSignatureException: If the time signature found in `note_sequence`
          has a denominator which is not a power of 2.
      NegativeTimeException: If a note or chord occurs at a negative time.
    """
    self._reset()

    self.steps_per_quarter = steps_per_quarter

    if note_sequence.time_signatures:
      time_signatures = sorted(note_sequence.time_signatures,
                               key=lambda ts: ts.time)
      # There is an implicit 4/4 time signature at 0 time. So if the first time
      # signature is something other than 4/4 and it's at a time other than 0,
      # that's an implicit time signature change.
      if time_signatures[0].time != 0 and not (
          time_signatures[0].numerator == 4 and
          time_signatures[0].denominator == 4):
        raise MultipleTimeSignatureException(
            'NoteSequence has an implicit change from initial 4/4 time '
            'signature.')

      self.time_signature = QuantizedSequence.TimeSignature(
          time_signatures[0].numerator, time_signatures[0].denominator)

      for time_signature in time_signatures[1:]:
        if (time_signature.numerator != self.time_signature.numerator or
            time_signature.denominator != self.time_signature.denominator):
          raise MultipleTimeSignatureException(
              'NoteSequence has at least one time signature change.')

    if not is_power_of_2(self.time_signature.denominator):
      raise BadTimeSignatureException(
          'Denominator is not a power of 2. Time signature: %d/%d' %
          (self.time_signature.numerator, self.time_signature.denominator))

    if note_sequence.tempos:
      tempos = sorted(note_sequence.tempos, key=lambda t: t.time)
      # There is an implicit 120.0 qpm tempo at 0 time. So if the first tempo is
      # something other that 120.0 and it's at a time other than 0, that's an
      # implicit tempo change.
      if tempos[0].time != 0 and tempos[0].qpm != 120.0:
        raise MultipleTempoException(
            'NoteSequence has an implicit tempo change from initial 120.0 qpm')

      self.qpm = tempos[0].qpm

      for tempo in tempos[1:]:
        if tempo.qpm != self.qpm:
          raise MultipleTempoException(
              'NoteSequence has at least one tempo change.')
    else:
      self.qpm = constants.DEFAULT_QUARTERS_PER_MINUTE

    # Compute quantization steps per second.
    steps_per_second = steps_per_quarter * self.qpm / 60.0

    quantize = lambda x: int(x + (1 - QUANTIZE_CUTOFF))

    self.total_steps = quantize(note_sequence.total_time * steps_per_second)

    for note in note_sequence.notes:
      # Quantize the start and end times of the note.
      start_step = quantize(note.start_time * steps_per_second)
      end_step = quantize(note.end_time * steps_per_second)
      if end_step == start_step:
        end_step += 1

      # Do not allow notes to start or end in negative time.
      if start_step < 0 or end_step < 0:
        raise NegativeTimeException(
            'Got negative note time: start_step = %s, end_step = %s' %
            (start_step, end_step))

      # Extend quantized sequence if necessary.
      if end_step > self.total_steps:
        self.total_steps = end_step

      if note.instrument not in self.tracks:
        self.tracks[note.instrument] = []
      self.tracks[note.instrument].append(
          QuantizedSequence.Note(pitch=note.pitch,
                                 velocity=note.velocity,
                                 start=start_step,
                                 end=end_step,
                                 instrument=note.instrument,
                                 program=note.program,
                                 is_drum=note.is_drum))

    # Also add chord symbol annotations to the quantized sequence.
    for annotation in note_sequence.text_annotations:
      if annotation.annotation_type == CHORD_SYMBOL:
        # Quantize the chord time, disallowing negative time.
        step = quantize(annotation.time * steps_per_second)
        if step < 0:
          raise NegativeTimeException(
              'Got negative chord time: step = %s' % step)
        self.chords.append(
            QuantizedSequence.ChordSymbol(step=step, figure=annotation.text))

  def __eq__(self, other):
    if not isinstance(other, QuantizedSequence):
      return False
    for track in self.tracks:
      if (track not in other.tracks or
          set(self.tracks[track]) != set(other.tracks[track])):
        return False
    return (
        self.qpm == other.qpm and
        self.time_signature == other.time_signature and
        self.steps_per_quarter == other.steps_per_quarter and
        self.total_steps == other.total_steps and
        set(self.chords) == set(other.chords))

  def __deepcopy__(self, unused_memo=None):
    new_copy = type(self)()
    new_copy.tracks = copy.deepcopy(self.tracks)
    new_copy.chords = copy.deepcopy(self.chords)
    new_copy.qpm = self.qpm
    new_copy.time_signature = self.time_signature
    new_copy.steps_per_quarter = self.steps_per_quarter
    new_copy.total_steps = self.total_steps
    return new_copy
