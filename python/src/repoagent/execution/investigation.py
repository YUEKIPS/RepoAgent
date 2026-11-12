"""Task notes are model assertions linked to host facts, never source authority."""
from pydantic import Field

from repoagent.execution.contracts import StrictModel, TaskFact, TaskObjective


class UpdateTaskArgs(StrictModel):
    objectives: list[TaskObjective] = Field(default_factory=list, max_length=16)
    facts: list[TaskFact] = Field(default_factory=list, max_length=24)


def update_task(state, evidence, *, objectives=None, facts=None):
    update = UpdateTaskArgs(objectives=objectives or [], facts=facts or [])
    if not update.objectives and not update.facts:
        raise ValueError("No task update")
    current = state.investigation
    known = {e.evidence_id for e in evidence}
    merged_facts = {f.fact_id: f for f in current.facts}
    for fact in update.facts:
        if not set(fact.evidence_ids) <= known or (fact.basis != "background" and not fact.evidence_ids):
            raise ValueError("Fact source is not registered")
        merged_facts[fact.fact_id] = fact
    merged_objectives = {o.objective_id: o for o in current.objectives}
    for objective in update.objectives:
        if not set(objective.fact_ids) <= merged_facts.keys():
            raise ValueError("Unknown task fact")
        if objective.status == "addressed" and not objective.fact_ids:
            raise ValueError("An addressed objective needs facts")
        merged_objectives[objective.objective_id] = objective
    candidate = type(current)(goal=current.goal, objectives=list(merged_objectives.values()),
                              facts=list(merged_facts.values()), revision=current.revision,
                              review_concerns=current.review_concerns, strategy_hint=current.strategy_hint)
    changed = candidate != current
    if changed:
        candidate.revision += 1
        state.investigation = candidate
    return {"revision": state.investigation.revision, "changed": changed,
            "updated_objective_ids": [o.objective_id for o in update.objectives],
            "updated_fact_ids": [f.fact_id for f in update.facts],
            "source_evidence": False, "semantic_verification": "model_notes_not_program_proof"}
