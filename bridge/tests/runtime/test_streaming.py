import unittest

from bocco_bridge.streaming import SentenceAssembler, complete_sentence_prefix


class SentenceAssemblerTests(unittest.TestCase):
    def test_sentences_split_on_japanese_terminators(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        chunks: list[str] = []
        for delta in ("おはよう。今日", "は晴れです！", "散歩しよう？いいね。"):
            chunks.extend(assembler.feed(delta))
        chunks.extend(assembler.flush())

        self.assertEqual(
            chunks, ["おはよう。", "今日は晴れです！", "散歩しよう？いいね。"]
        )

    def test_sentence_completes_only_when_terminator_arrives(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        self.assertEqual(assembler.feed("こんに"), ())
        self.assertEqual(assembler.feed("ちは"), ())
        self.assertEqual(assembler.feed("。元気"), ("こんにちは。",))
        self.assertEqual(assembler.flush(), ("元気",))

    def test_no_terminal_punctuation_flushes_single_chunk(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        self.assertEqual(assembler.feed("句点のないテキスト"), ())
        self.assertEqual(assembler.flush(), ("句点のないテキスト",))

    def test_remainder_concatenates_into_final_chunk(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        emitted = assembler.feed("一。二。三。四。五。")
        self.assertEqual(emitted, ("一。", "二。"))
        self.assertEqual(assembler.flush(), ("三。四。五。",))
        self.assertEqual(assembler.emitted, 3)

    def test_terminator_runs_stay_with_one_sentence(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        chunks = list(assembler.feed("ほんと！？"))
        # The run touches the end of the buffer, so the boundary waits for
        # the next delta before deciding the sentence is complete.
        self.assertEqual(chunks, [])
        chunks.extend(assembler.feed("すごいね。"))
        chunks.extend(assembler.flush())
        self.assertEqual(chunks, ["ほんと！？", "すごいね。"])

    def test_empty_deltas_and_whitespace_are_ignored(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        self.assertEqual(assembler.feed(""), ())
        self.assertEqual(assembler.feed("   "), ())
        self.assertEqual(assembler.feed("。"), ())
        self.assertEqual(assembler.flush(), ())
        self.assertEqual(assembler.emitted, 0)

    def test_single_chunk_mode_defers_everything_to_flush(self) -> None:
        assembler = SentenceAssembler(max_chunks=1)
        self.assertEqual(assembler.feed("一。二。"), ())
        self.assertEqual(assembler.flush(), ("一。二。",))

    def test_invalid_max_chunks_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SentenceAssembler(max_chunks=0)

    def test_complete_only_flush_drops_the_unfinished_tail(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        self.assertEqual(assembler.feed("はい。まだ途中の文"), ("はい。",))
        self.assertEqual(assembler.flush(complete_only=True), ())

    def test_complete_only_flush_keeps_finished_sentences_in_the_tail(
        self,
    ) -> None:
        assembler = SentenceAssembler(max_chunks=2)
        self.assertEqual(assembler.feed("一。二。まだ途中"), ("一。",))
        self.assertEqual(assembler.flush(complete_only=True), ("二。",))

    def test_complete_only_flush_keeps_whole_sentences(self) -> None:
        assembler = SentenceAssembler(max_chunks=1)
        self.assertEqual(assembler.feed("一。二。"), ())
        self.assertEqual(assembler.flush(complete_only=True), ("一。二。",))

    def test_complete_only_flush_yields_nothing_without_a_terminator(
        self,
    ) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        self.assertEqual(assembler.feed("終わらなかった文"), ())
        self.assertEqual(assembler.flush(complete_only=True), ())
        self.assertEqual(assembler.emitted, 0)

    def test_complete_only_flush_empties_the_buffer(self) -> None:
        assembler = SentenceAssembler(max_chunks=3)
        assembler.feed("はい。切れた")
        assembler.flush(complete_only=True)
        self.assertEqual(assembler.flush(), ())


class CompleteSentencePrefixTests(unittest.TestCase):
    def test_keeps_text_through_the_last_terminator(self) -> None:
        self.assertEqual(
            complete_sentence_prefix("一です。二です！途中"), "一です。二です！"
        )

    def test_complete_text_is_unchanged(self) -> None:
        self.assertEqual(complete_sentence_prefix("終わり。"), "終わり。")

    def test_text_without_a_terminator_yields_nothing(self) -> None:
        self.assertEqual(complete_sentence_prefix("終わらない"), "")
        self.assertEqual(complete_sentence_prefix(""), "")

    def test_terminator_run_is_kept_whole(self) -> None:
        self.assertEqual(complete_sentence_prefix("ほんと！？あの"), "ほんと！？")


if __name__ == "__main__":
    unittest.main()
