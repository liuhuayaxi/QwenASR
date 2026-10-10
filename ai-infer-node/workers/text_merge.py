"""Without token/audio alignment, all raw segment contributions are preserved."""

def merge_text(previous, current, allow_overlap=True):
    # Text similarity cannot prove that two phrases are the same audio event.
    # Keep intentional and periodic repetition, even on overlapping windows.
    return previous + '\n' + current if previous else current
