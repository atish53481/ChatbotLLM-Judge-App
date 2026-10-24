"""Common shape every target connector implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChatReply:
    reply: str
    model: str
    mode: str  # "http"
    # What the chatbot's retriever returned, when its response carries it (RAG metrics).
    retrieval_context: list[str] | None = None
    # The tools the chatbot (an agent) called for this reply, when its response
    # lists them (agent metrics): [{name, input_parameters?, output?, reasoning?}].
    tools_called: list[dict] | None = None
    # Seconds the chatbot took, when a wrapper timed it (shared.SharedReplies).
    latency_s: float | None = None


class ChatbotClient(ABC):
    @abstractmethod
    def chat(self, message: str, history: list[dict] | None = None) -> ChatReply:
        ...
