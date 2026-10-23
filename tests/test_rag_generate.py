from unittest.mock import MagicMock, patch


def test_generate_goldens_from_document_saves_goldens(tmp_path, monkeypatch):
    from backend.datasets import goldens as goldens_store

    goldens_path = tmp_path / "goldens.json"
    goldens_path.write_text("[]")
    monkeypatch.setattr(goldens_store, "GOLDENS_PATH", str(goldens_path))

    from backend.rag import generate

    fake_golden = MagicMock(
        input="What is the refund window?",
        expected_output="7 days.",
        context=["Refunds must be requested within 7 days."],
    )
    with patch("backend.rag.generate.Synthesizer") as MockSynthesizer, \
         patch("backend.rag.generate.LocalSentenceEmbedder"), \
         patch("backend.rag.generate.ContextConstructionConfig"):
        MockSynthesizer.return_value.generate_goldens_from_docs.return_value = [fake_golden]
        count = generate.generate_goldens_from_document(
            "doc.pdf", "general_support", "doc.pdf", judge=MagicMock()
        )

    assert count == 1
    saved = goldens_store.load_goldens(theme="general_support")
    assert len(saved) == 1
    assert saved[0]["question"] == "What is the refund window?"
    assert saved[0]["expected_answer"] == "7 days."
    assert saved[0]["context"] == ["Refunds must be requested within 7 days."]
    assert saved[0]["source"] == "synthesized"
    assert saved[0]["source_document"] == "doc.pdf"


def test_generate_goldens_handles_missing_expected_output(tmp_path, monkeypatch):
    from backend.datasets import goldens as goldens_store

    goldens_path = tmp_path / "goldens.json"
    goldens_path.write_text("[]")
    monkeypatch.setattr(goldens_store, "GOLDENS_PATH", str(goldens_path))

    from backend.rag import generate

    fake_golden = MagicMock(input="Q?", expected_output=None, context=None)
    with patch("backend.rag.generate.Synthesizer") as MockSynthesizer, \
         patch("backend.rag.generate.LocalSentenceEmbedder"), \
         patch("backend.rag.generate.ContextConstructionConfig"):
        MockSynthesizer.return_value.generate_goldens_from_docs.return_value = [fake_golden]
        generate.generate_goldens_from_document("doc.txt", "t", "doc.txt", judge=MagicMock())

    saved = goldens_store.load_goldens(theme="t")
    assert saved[0]["expected_answer"] == ""
    assert saved[0]["context"] == []


def test_generate_goldens_passes_judge_as_critic_model(tmp_path, monkeypatch):
    """ContextConstructionConfig resolves critic_model in __post_init__, at
    construction time — leaving it unset makes DeepEval silently try to
    build a default OpenAI model and demand OPENAI_API_KEY, defeating the
    "reuse the judge, no second key" requirement. Regression test for that."""
    from backend.datasets import goldens as goldens_store

    goldens_path = tmp_path / "goldens.json"
    goldens_path.write_text("[]")
    monkeypatch.setattr(goldens_store, "GOLDENS_PATH", str(goldens_path))

    from backend.rag import generate

    judge = MagicMock()
    with patch("backend.rag.generate.Synthesizer") as MockSynthesizer, \
         patch("backend.rag.generate.LocalSentenceEmbedder"), \
         patch("backend.rag.generate.ContextConstructionConfig") as MockConfig:
        MockSynthesizer.return_value.generate_goldens_from_docs.return_value = []
        generate.generate_goldens_from_document("doc.txt", "t", "doc.txt", judge=judge)

    _, kwargs = MockConfig.call_args
    assert kwargs["critic_model"] is judge


def test_generate_goldens_makes_two_by_default_and_honours_max_goldens(tmp_path, monkeypatch):
    """A document gives 2 goldens unless asked for more (DeepEval alone makes up
    to 3 contexts x 2), and never more than asked."""
    from backend.datasets import goldens as goldens_store

    goldens_path = tmp_path / "goldens.json"
    goldens_path.write_text("[]")
    monkeypatch.setattr(goldens_store, "GOLDENS_PATH", str(goldens_path))

    from backend.rag import generate

    def golden(n):
        return MagicMock(input=f"Q{n}?", expected_output=f"A{n}.", context=[f"fact {n}"],
                         additional_metadata={})

    for options, contexts, per_context, saved in (({}, 1, 2, 2), ({"max_goldens": 1}, 1, 1, 1),
                                                   ({"max_goldens": 5}, 3, 2, 5)):
        goldens_path.write_text("[]")
        with patch("backend.rag.generate.Synthesizer") as MockSynthesizer, \
             patch("backend.rag.generate.LocalSentenceEmbedder"), \
             patch("backend.rag.generate.ContextConstructionConfig") as MockConfig:
            MockSynthesizer.return_value.generate_goldens_from_docs.return_value = [golden(n) for n in range(6)]
            created = generate.generate_goldens_from_document("doc.txt", "t", "doc.txt", judge=MagicMock(),
                                                              options=options)
        assert MockConfig.call_args.kwargs["max_contexts_per_document"] == contexts
        call = MockSynthesizer.return_value.generate_goldens_from_docs.call_args
        assert call.kwargs["max_goldens_per_context"] == per_context
        assert created == saved == len(goldens_store.load_goldens(theme="t"))
