from time import perf_counter

from .models import AgentAction, AgentStep, AgentTrace, Citation


MAX_DECISIONS = 4
AGENT_TIMEOUT_SECONDS = 85


async def collect_evidence(question: str, document_ids: list[str], store, provider) -> tuple[list[Citation], AgentTrace]:
    evidence: dict[int, Citation] = {}
    evidence_index: dict[tuple[str, int], int] = {}
    steps: list[AgentStep] = []
    queries: dict[str, str] = {}
    read_sources: set[int] = set()
    scope = set(document_ids)
    allow_search = True

    for index in range(MAX_DECISIONS):
        started = perf_counter()
        actions: list[AgentAction] = ["finish"] if evidence else []
        if index < MAX_DECISIONS - 1:
            if any(entry._has_adjacent and identifier not in read_sources
                   for identifier, entry in evidence.items()):
                actions.append("read_section")
            if allow_search:
                actions.append("search_docs")
        actions.append("insufficient_evidence")
        decision = await provider.choose(question, evidence, steps, list(queries.values()), actions)
        rows = []
        ids = []
        outcome = "ok"

        if decision.action not in actions:
            outcome = "invalid_arguments"
        elif decision.action == "search_docs":
            query = " ".join(decision.query.split())
            if not query:
                outcome = "invalid_arguments"
            elif query.casefold() in queries:
                outcome = "duplicate"
                allow_search = False
            else:
                queries[query.casefold()] = query
                vector = (await provider.embed([query]))[0]
                rows = await store.search(vector, document_ids, include_adjacent=True)
        elif decision.action == "read_section":
            source = evidence.get(decision.source_id)
            if source is None or not source._has_adjacent:
                outcome = "invalid_arguments"
            elif decision.source_id in read_sources:
                outcome = "duplicate"
            else:
                read_sources.add(decision.source_id)
                rows = await store.read_section(source)
        elif decision.action == "finish":
            ids = list(dict.fromkeys(decision.source_ids))
            if not ids or any(identifier not in evidence for identifier in ids):
                outcome = "invalid_arguments"
                ids = []

        if decision.action in ("search_docs", "read_section") and outcome == "ok":
            for row in rows:
                if scope and row.document_id not in scope:
                    continue
                key = (row.document_id, row.chunk_index)
                identifier = evidence_index.get(key)
                if identifier is None:
                    identifier = len(evidence) + 1
                    evidence_index[key] = identifier
                    evidence[identifier] = row
                ids.append(identifier)
            if not ids:
                outcome = "empty"

        steps.append(AgentStep(action=decision.action, outcome=outcome, evidence_ids=ids,
                               duration_ms=round((perf_counter() - started) * 1000, 3)))
        if outcome == "ok" and decision.action == "finish":
            # Final generation uses its own contiguous [S1..Sn] numbering.
            return [evidence[identifier] for identifier in ids], AgentTrace(
                steps=steps, stop_reason="finished", selected_evidence_ids=ids)
        if outcome == "ok" and decision.action == "insufficient_evidence":
            return [], AgentTrace(steps=steps, stop_reason="insufficient_evidence", selected_evidence_ids=[])
    return [], AgentTrace(steps=steps, stop_reason="step_limit", selected_evidence_ids=[])
