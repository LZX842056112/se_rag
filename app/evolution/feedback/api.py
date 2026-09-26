"""
反馈 API：POST /evolution/feedback。
默认 EVOLUTION_ENABLED=false 时仅幂等接受、不落库，不影响主查询。
"""
from fastapi import APIRouter

from app.evolution.feedback.collector import record_feedback
from app.evolution.models import FeedbackEvent
from app.evolution.schema import OkResponse

feedback_router = APIRouter(prefix="/evolution", tags=["evolution-feedback"])


@feedback_router.post("/feedback", response_model=OkResponse)
def post_feedback(req: FeedbackEvent) -> OkResponse:
    record_feedback(req)
    return OkResponse(code=200, message="feedback accepted")