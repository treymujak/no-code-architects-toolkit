# Copyright (c) 2025 Stephen G. Pope
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.



import os
import tempfile
import ffmpeg
import srt
from datetime import timedelta
from openai import OpenAI
from services.file_management import download_file
import logging
import uuid

# Set up logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Set the default local storage directory
STORAGE_PATH = "/tmp/"


def _extract_audio_for_openai(media_path):
    """Strip media to a 64kbps mono mp3 to stay under OpenAI's 25MB upload limit."""
    fd, audio_path = tempfile.mkstemp(suffix=".mp3", dir=STORAGE_PATH)
    os.close(fd)
    (
        ffmpeg
        .input(media_path)
        .output(audio_path, vn=None, acodec="libmp3lame", audio_bitrate="64k", ac=1)
        .overwrite_output()
        .run(quiet=True)
    )
    return audio_path


def _bucket_words_into_segments(words, segments):
    """OpenAI returns a flat word list; reshape into per-segment word lists."""
    buckets = [[] for _ in segments]
    if not words:
        return buckets
    i = 0
    for w in words:
        while i < len(segments) - 1 and w.start >= segments[i].end:
            i += 1
        buckets[i].append({"word": w.word, "start": w.start, "end": w.end})
    return buckets


def _openai_transcribe(audio_path, language=None, want_words=False):
    """Call the OpenAI Whisper API and return a dict shaped like the local Whisper result."""
    client = OpenAI()
    kwargs = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": ["word", "segment"] if want_words else ["segment"],
    }
    if language:
        kwargs["language"] = language
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(file=f, **kwargs)

    if want_words:
        bucketed = _bucket_words_into_segments(response.words or [], response.segments)
        segments = [
            {"start": s.start, "end": s.end, "text": s.text, "words": bucketed[idx]}
            for idx, s in enumerate(response.segments)
        ]
    else:
        segments = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in (response.segments or [])
        ]
    return {"text": response.text, "segments": segments}


def process_transcription(media_url, output_type, max_chars=56, language=None,):
    """Transcribe media and return the transcript, SRT or ASS file path."""
    logger.info(f"Starting transcription for media URL: {media_url} with output type: {output_type}")
    input_filename = download_file(media_url, os.path.join(STORAGE_PATH, 'input_media'))
    logger.info(f"Downloaded media to local file: {input_filename}")

    audio_path = None
    try:
        audio_path = _extract_audio_for_openai(input_filename)
        logger.info(f"Extracted audio to {audio_path} for OpenAI Whisper API")

        if output_type == 'transcript':
            result = _openai_transcribe(audio_path, language=language)
            output = result['text']
            logger.info("Generated transcript output")
        elif output_type in ['srt', 'vtt']:
            result = _openai_transcribe(audio_path, language=language)
            srt_subtitles = []
            for i, segment in enumerate(result['segments'], start=1):
                start = timedelta(seconds=segment['start'])
                end = timedelta(seconds=segment['end'])
                text = segment['text'].strip()
                srt_subtitles.append(srt.Subtitle(i, start, end, text))

            output_content = srt.compose(srt_subtitles)

            output_filename = os.path.join(STORAGE_PATH, f"{uuid.uuid4()}.{output_type}")
            with open(output_filename, 'w') as f:
                f.write(output_content)

            output = output_filename
            logger.info(f"Generated {output_type.upper()} output: {output}")

        elif output_type == 'ass':
            result = _openai_transcribe(audio_path, language=language, want_words=True)
            logger.info("Transcription completed with word-level timestamps")
            ass_content = generate_ass_subtitle(result, max_chars)
            logger.info("Generated ASS subtitle content")

            output_content = ass_content

            output_filename = os.path.join(STORAGE_PATH, f"{uuid.uuid4()}.{output_type}")
            with open(output_filename, 'w') as f:
                f.write(output_content)
            output = output_filename
            logger.info(f"Generated {output_type.upper()} output: {output}")
        else:
            raise ValueError("Invalid output type. Must be 'transcript', 'srt', or 'vtt'.")

        os.remove(input_filename)
        logger.info(f"Removed local file: {input_filename}")
        logger.info(f"Transcription successful, output type: {output_type}")
        return output
    except Exception as e:
        logger.error(f"Transcription failed: {str(e)}")
        raise
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass


def generate_ass_subtitle(result, max_chars):
    """Generate ASS subtitle content with highlighted current words, showing one line at a time."""
    logger.info("Generate ASS subtitle content with highlighted current words")
    # ASS file header
    ass_content = ""

    # Helper function to format time
    def format_time(t):
        hours = int(t // 3600)
        minutes = int((t % 3600) // 60)
        seconds = int(t % 60)
        centiseconds = int(round((t - int(t)) * 100))
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

    max_chars_per_line = max_chars  # Maximum characters per line

    # Process each segment
    for segment in result['segments']:
        words = segment.get('words', [])
        if not words:
            continue  # Skip if no word-level timestamps

        # Group words into lines
        lines = []
        current_line = []
        current_line_length = 0
        for word_info in words:
            word_length = len(word_info['word']) + 1  # +1 for space
            if current_line_length + word_length > max_chars_per_line:
                lines.append(current_line)
                current_line = [word_info]
                current_line_length = word_length
            else:
                current_line.append(word_info)
                current_line_length += word_length
        if current_line:
            lines.append(current_line)

        # Generate events for each line
        for line in lines:
            line_start_time = line[0]['start']
            line_end_time = line[-1]['end']

            # Generate events for highlighting each word
            for i, word_info in enumerate(line):
                start_time = word_info['start']
                end_time = word_info['end']
                current_word = word_info['word']

                # Build the line text with highlighted current word
                caption_parts = []
                for w in line:
                    word_text = w['word']
                    if w == word_info:
                        # Highlight current word
                        caption_parts.append(r'{\c&H00FFFF&}' + word_text)
                    else:
                        # Default color
                        caption_parts.append(r'{\c&HFFFFFF&}' + word_text)
                caption_with_highlight = ' '.join(caption_parts)

                # Format times
                start = format_time(start_time)
                # End the dialogue event when the next word starts or at the end of the line
                if i + 1 < len(line):
                    end_time = line[i + 1]['start']
                else:
                    end_time = line_end_time
                end = format_time(end_time)

                # Add the dialogue line
                ass_content += f"Dialogue: 0,{start},{end},Default,,0,0,0,,{caption_with_highlight}\n"

    return ass_content