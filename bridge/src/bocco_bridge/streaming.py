"""Incremental sentence chunking for streamed conversational replies."""

from __future__ import annotations


SENTENCE_TERMINATORS = frozenset("。！？")


def complete_sentence_prefix(text: str) -> str:
    """Return ``text`` up to and including its last sentence terminator.

    Whatever follows the final 。！？ is a sentence the model never got to
    finish. When the generation is known to have been cut short, speaking
    that fragment would present half a sentence as a complete answer, so
    callers drop it. Text with no terminator at all yields ``""``.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    for index in range(len(text) - 1, -1, -1):
        if text[index] in SENTENCE_TERMINATORS:
            return text[: index + 1]
    return ""


def _speakable(text: str) -> bool:
    """True when the text contains something beyond punctuation to speak."""

    return any(
        not character.isspace() and character not in SENTENCE_TERMINATORS
        for character in text
    )


class SentenceAssembler:
    """Split streamed text deltas into at most ``max_chunks`` utterances.

    Completed sentences (ending in 。！？) are emitted as soon as they
    arrive, up to ``max_chunks - 1`` of them; everything that follows is
    buffered and returned by :meth:`flush` as the final chunk, so the reply
    never exceeds ``max_chunks`` messages.
    """

    def __init__(self, max_chunks: int = 3) -> None:
        if max_chunks < 1:
            raise ValueError("max_chunks must be at least 1")
        self._max_chunks = max_chunks
        self._buffer = ""
        self._emitted = 0

    @property
    def emitted(self) -> int:
        return self._emitted

    def feed(self, delta: str) -> tuple[str, ...]:
        """Absorb one delta and return any newly completed sentences."""

        if not isinstance(delta, str) or not delta:
            return ()
        self._buffer += delta
        completed: list[str] = []
        while self._emitted < self._max_chunks - 1:
            boundary = self._sentence_boundary()
            if boundary is None:
                break
            sentence = self._buffer[:boundary].strip()
            self._buffer = self._buffer[boundary:]
            if _speakable(sentence):
                completed.append(sentence)
                self._emitted += 1
        return tuple(completed)

    def flush(self, *, complete_only: bool = False) -> tuple[str, ...]:
        """Return the remaining buffered text as the final chunk, if any.

        With ``complete_only`` the trailing fragment after the last
        sentence terminator is discarded. Callers pass it when the stream
        ended abnormally — cut off at the token budget or dropped mid
        flight — because the tail is then an unfinished sentence rather
        than the end of the reply.
        """

        remainder = self._buffer
        self._buffer = ""
        if complete_only:
            remainder = complete_sentence_prefix(remainder)
        remainder = remainder.strip()
        if not _speakable(remainder):
            return ()
        self._emitted += 1
        return (remainder,)

    def _sentence_boundary(self) -> int | None:
        """Find the end of the first completed sentence in the buffer.

        A run of consecutive terminators (「！？」 etc.) is kept together;
        if that run touches the end of the buffer the boundary is deferred
        until the next delta proves the run is over (flush() handles the
        stream ending there).
        """

        for position, character in enumerate(self._buffer):
            if character not in SENTENCE_TERMINATORS:
                continue
            end = position + 1
            while (
                end < len(self._buffer)
                and self._buffer[end] in SENTENCE_TERMINATORS
            ):
                end += 1
            if end == len(self._buffer):
                return None
            return end
        return None
