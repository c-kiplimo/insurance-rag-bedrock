"""In-memory fakes for every port, so the pipeline runs in unit tests without AWS."""

from __future__ import annotations

from insurance_rag.config import Settings
from insurance_rag.models import Chunk, ChunkType, QueryResponse, RetrievedChunk
from insurance_rag.orchestrator.pipeline import Dependencies, RagPipeline
from insurance_rag.orchestrator.rewrite import QueryRewriter
from insurance_rag.ports import GuardrailVerdict, LLMResult, PromptTemplate
from insurance_rag.redaction import RegexRedactor


def make_chunk(
    i: int, text: str, kind: ChunkType = ChunkType.CLAUSE, section: str = "sec1"
) -> Chunk:
    return Chunk(
        chunk_id=f"c{i}",
        doc_id="doc",
        doc_title="Test Policy",
        section_id=section,
        section_path="III. Coverage > A. Building",
        ordinal=i,
        chunk_type=kind,
        text=text,
        page_start=3,
    )


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [1.0, 0.0, 0.0]


class FakeLLM:
    """Returns the rewrite JSON for rewrite prompts and ``answer`` for everything else."""

    def __init__(self, answer: str = "Proof of loss is due within 60 days [S1].") -> None:
        self.answer = answer
        self.calls: list[str] = []

    def generate(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int,
        temperature: float,
        model_id: str | None = None,
    ) -> LLMResult:
        if "prepare search queries" in system:
            self.calls.append("rewrite")
            return LLMResult('{"query": "proof of loss deadline", "hyde": null}')
        self.calls.append("answer")
        return LLMResult(self.answer, 100, 20)


class FakeReranker:
    def __init__(self, score: float = 0.9) -> None:
        self.score = score

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        return [(i, self.score - i * 0.01) for i in range(min(top_n, len(documents)))]


class FakeRetriever:
    def __init__(
        self, hits: list[RetrievedChunk], extra: list[RetrievedChunk] | None = None
    ) -> None:
        self.hits = hits
        self.extra = extra or []
        self.searched = 0

    def search(self, query_text: str, vector: list[float], k: int) -> list[RetrievedChunk]:
        self.searched += 1
        return self.hits

    def expand(self, hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
        return self.extra


class FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, QueryResponse] = {}
        self.puts = 0

    def get(self, question: str, vector: list[float]) -> QueryResponse | None:
        return self.store.get(question)

    def put(self, question: str, vector: list[float], response: QueryResponse) -> None:
        self.puts += 1
        self.store[question] = response


class FakeGuardrails:
    def __init__(self, block_input: bool = False, block_output: bool = False) -> None:
        self.block_input = block_input
        self.block_output = block_output

    def check_input(self, text: str) -> GuardrailVerdict:
        return GuardrailVerdict(blocked=self.block_input, text=text)

    def check_output(self, answer: str, question: str, sources: list[str]) -> GuardrailVerdict:
        return GuardrailVerdict(blocked=self.block_output, text=answer)


class FakePrompts:
    def get(self) -> PromptTemplate:
        return PromptTemplate("t", "1", "SYSTEM {{question}}", "<s>{{context}}</s> Q: {{question}}")


def build_pipeline(
    *,
    llm: FakeLLM | None = None,
    retriever: FakeRetriever | None = None,
    cache: FakeCache | None = None,
    guardrails: FakeGuardrails | None = None,
    reranker: FakeReranker | None = None,
    settings: Settings | None = None,
) -> tuple[RagPipeline, dict[str, object]]:
    llm = llm or FakeLLM()
    retriever = retriever or FakeRetriever(
        [RetrievedChunk(make_chunk(1, "Proof of loss must be filed within 60 days."), 1.0)]
    )
    cache = cache or FakeCache()
    embedder = FakeEmbedder()
    deps = Dependencies(
        embedder=embedder,
        llm=llm,
        reranker=reranker or FakeReranker(),
        guardrails=guardrails or FakeGuardrails(),
        retriever=retriever,
        cache=cache,
        prompts=FakePrompts(),
        rewriter=QueryRewriter(llm, None),
        redactor=RegexRedactor(),
    )
    pipeline = RagPipeline(deps, settings or Settings())
    return pipeline, {"llm": llm, "retriever": retriever, "cache": cache, "embedder": embedder}
