"""Model-generated history memory with verified provenance and bounded coverage."""

import hashlib
import json

from repoagent.execution.contracts import (
    ConversationSummary,
    ModelRequest,
    SummaryRequest,
)
from repoagent.execution.prompt_spec import HISTORY_SUMMARY
from repoagent.models.base import ModelServiceError


class SummaryService:
    def needs_summary(self, material, limits):
        return bool(limits.summary_enabled and material and material.get("turns"))

    def build_request(self, query, material):
        material = SummaryRequest.model_validate(material)
        if material.previous_summary and (material.previous_summary.session_id != query.conversation.session_id or material.previous_summary.workspace_version != query.workspace.version):
            raise ModelServiceError(code="invalid_summary_provenance")
        return ModelRequest(
            stage="summary", query=query.model_copy(update={"conversation": None}),
            instruction=HISTORY_SUMMARY,
            summary_input=material,
        )

    def summarize(self, session, query, material):
        request = self.build_request(query, material)
        prepared = session.prepare(request)
        if prepared.input_size > session.max_input or not prepared.measurement.get("hard_fits", True):
            session.discard(prepared)
            return None
        result = session.invoke(prepared).response.summary
        return self.validate_result(result, query, material, session.generation_id)

    def validate_result(self, result, query, material, generation_id):
        allowed = set(material["source_turn_ids"])
        if not result.items or any(not set(item.turn_ids) <= allowed for item in result.items):
            raise ModelServiceError(code="invalid_summary_provenance")
        source_hash = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return ConversationSummary(
            revision=source_hash, session_id=query.conversation.session_id,
            workspace_version=query.workspace.version,
            covered_through_sequence=material["covered_through_sequence"],
            source_turn_ids=material["source_turn_ids"], source_hash=source_hash,
            generation_id=generation_id, result=result,
        )
