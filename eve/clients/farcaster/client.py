from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import hmac
import hashlib
import logging
import os
from farcaster import Warpcast
from dotenv import load_dotenv

from eve.agent import Agent
from eve.llm import UserMessage, async_prompt_thread
from eve.thread import Thread
from eve.tool import get_tools_from_mongo
from eve.user import User

logger = logging.getLogger(__name__)


class MentionedProfile(BaseModel):
    fid: int
    username: str
    custody_address: str
    display_name: str
    pfp_url: str


class CastWebhook(BaseModel):
    created_at: int
    type: str
    data: dict


def create_app(client: Warpcast, agent: Agent, db: str = "STAGE"):
    app = FastAPI()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def verify_neynar_signature(signature: str, raw_body: bytes) -> bool:
        webhook_secret = os.environ.get("CLIENT_FARCASTER_NEYNAR_WEBHOOK_SECRET")
        computed_signature = hmac.new(
            webhook_secret.encode(), raw_body, hashlib.sha512
        ).hexdigest()
        return hmac.compare_digest(computed_signature, signature)

    @app.post("/")
    async def handle_webhook(request: Request):
        body = await request.body()

        signature = request.headers.get("X-Neynar-Signature")
        if not signature:
            raise HTTPException(status_code=400, detail="Missing signature header")

        if not verify_neynar_signature(signature, body):
            raise HTTPException(status_code=400, detail="Invalid signature")

        webhook_data = CastWebhook.model_validate(await request.json())

        if webhook_data.type != "cast.created":
            return {"status": "ignored", "message": "Not a cast.created event"}

        try:
            cast_data = webhook_data.data
            if not cast_data or "hash" not in cast_data:
                raise HTTPException(status_code=400, detail="Invalid cast data")

            cast_hash = cast_data["hash"]
            author = cast_data["author"]
            author_username = author["username"]
            author_fid = author["fid"]

            # Get or create user
            user = User.from_farcaster(author_fid, author_username, db=db)

            # Get or create thread
            thread_key = f"farcaster-{author_fid}-{cast_hash}"
            thread = Thread.get_collection(db).find_one({"key": thread_key})
            if not thread:
                thread = Thread.create(key=thread_key, db=db)
            thread_id = thread.get("_id") if isinstance(thread, dict) else thread.id

            # Create message
            user_message = UserMessage(
                content=cast_data["text"],
                name=author_username,
            )

            # Get response from Eve
            tools = get_tools_from_mongo(db=db)
            reply_text = None
            async for msg in async_prompt_thread(
                db=db,
                user_id=user.id,
                agent_id=agent.id,
                thread_id=thread_id,
                user_messages=user_message,
                tools=tools,
            ):
                if msg.type == "assistant_message" and msg.message.content:
                    reply_text = msg.message.content
                    break

            if reply_text:
                client.post_cast(
                    text=reply_text, parent={"hash": cast_hash, "fid": author_fid}
                )

            return {
                "status": "success",
                "message": "Reply sent successfully",
                "cast_hash": cast_hash,
            }

        except Exception as e:
            logger.error(f"Error handling webhook: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))

    return app


def start(env=None):
    """Start the FastAPI server locally"""
    import uvicorn

    if env:
        load_dotenv(env)

    mnemonic = os.environ.get("CLIENT_FARCASTER_MNEMONIC")
    agent_key = os.environ.get("CLIENT_AGENT_KEY", "eve")
    db = os.environ.get("DB", "STAGE")

    client = Warpcast(mnemonic=mnemonic)
    agent = Agent.load(agent_key, db=db)
    logger.info("Initialized Farcaster client")

    app = create_app(client, agent, db=db)
    uvicorn.run(app, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    start()
