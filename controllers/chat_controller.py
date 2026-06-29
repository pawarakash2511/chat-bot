import logging

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse

from schemas.chat import ChatRequest
from services.chat_service import conversation, stream_conversation

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/chat")
def chat_controller(
    request: ChatRequest,
    x_user_id: str = Header(default="anonymous"),
):
    try:
        answer = conversation(user_id=x_user_id, q=request.q)
        logger.info("Chat response generated for user %s", x_user_id)
        return {"status": "success", "data": answer}
    except Exception:
        logger.exception("Chat failed for user %s", x_user_id)
        raise HTTPException(status_code=500, detail="Failed to generate a response")


@router.post("/chat/stream")
async def chat_stream_controller(
    request: ChatRequest,
    x_user_id: str = Header(default="anonymous"),
):
    return StreamingResponse(
        stream_conversation(user_id=x_user_id, q=request.q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
