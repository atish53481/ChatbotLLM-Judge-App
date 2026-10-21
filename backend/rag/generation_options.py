"""Names of the optional golden-generation settings, importable without the
Synthesizer (backend/rag/generate.py pulls in sentence-transformers, ~40s)."""

# Evol-Instruct rewrites that make generated questions harder or broader.
EVOLUTION_NAMES = (
    "reasoning", "multi_context", "concretizing", "constrained",
    "comparative", "hypothetical", "in_breadth",
)
STYLING_FIELDS = ("scenario", "task", "input_format", "expected_output_format")
