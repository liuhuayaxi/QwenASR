"""雨课堂 (Yuketang) question-bank importer.

Everything runs locally: a persistent Playwright Chrome profile keeps the
student's own login, sync performs polite read-only GETs, and the normalized
bank is stored per classroom under the private QwenASR data directory. Course
bindings link a QwenASR course (category) to one or more classroom ids so note
generation can retrieve the relevant questions.
"""
