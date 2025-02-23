import re
import os
import json
import asyncio
import traceback
import functools
import openai
import anthropic
from enum import Enum
from bson import ObjectId
from typing import Optional, Dict, Any, List, Union, Literal, Tuple, AsyncGenerator
from pydantic import BaseModel, Field
from pydantic.config import ConfigDict
from instructor.function_calls import openai_schema
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception
from sentry_sdk import trace, start_transaction, add_breadcrumb, capture_exception

from .eden_utils import dump_json, load_template
from .tool import Tool, BASE_TOOLS, TOOL_CATEGORIES
from .task import Creation
from .user import User
from .agent import Agent, refresh_agent
from .thread import UserMessage, AssistantMessage, ToolCall, Thread
from .api.rate_limiter import RateLimiter


USE_RATE_LIMITS = os.getenv("USE_RATE_LIMITS", "false").lower() == "true"
USE_THINKING = os.getenv("USE_THINKING", "false").lower() == "true"


class UpdateType(str, Enum):
    START_PROMPT = "start_prompt"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_COMPLETE = "tool_complete"
    ERROR = "error"
    END_PROMPT = "end_prompt"
    ASSISTANT_TOKEN = "assistant_token"
    ASSISTANT_STOP = "assistant_stop"
    TOOL_CALL = "tool_call"


models = ["claude-3-5-sonnet-20241022", "gpt-4o-mini", "gpt-4o-2024-08-06"]
DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


system_template = load_template("system")
knowledge_think_template = load_template("knowledge_think") 
knowledge_reply_template = load_template("knowledge_reply")
thought_template = load_template("thought")
tools_template = load_template("tools")


async def async_anthropic_prompt(
    messages: List[Union[UserMessage, AssistantMessage]],
    system_message: Optional[str],
    model: Literal[tuple(models)] = "claude-3-5-haiku-20241022",
    response_model: Optional[type[BaseModel]] = None,
    tools: Dict[str, Tool] = {},
):
    anthropic_client = anthropic.AsyncAnthropic()

    prompt = {
        "model": model,
        "max_tokens": 8192,
        "messages": [item for msg in messages for item in msg.anthropic_schema()],
        "system": system_message,
    }

    if tools or response_model:
        tool_schemas = [
            t.anthropic_schema(exclude_hidden=True) for t in (tools or {}).values()
        ]
        if response_model:
            tool_schemas.append(openai_schema(response_model).anthropic_schema)
            prompt["tool_choice"] = {"type": "tool", "name": response_model.__name__}
        prompt["tools"] = tool_schemas

    response = await anthropic_client.messages.create(**prompt)

    if response_model:
        return response_model(**response.content[0].input)
    else:
        content = ". ".join(
            [r.text for r in response.content if r.type == "text" and r.text]
        )
        tool_calls = [
            ToolCall.from_anthropic(r) for r in response.content if r.type == "tool_use"
        ]
        stop = response.stop_reason == "end_turn"
        return content, tool_calls, stop


async def async_anthropic_prompt_stream(
    messages: List[Union[UserMessage, AssistantMessage]],
    system_message: Optional[str],
    model: Literal[tuple(models)] = "claude-3-5-haiku-20241022",
    response_model: Optional[type[BaseModel]] = None,
    tools: Dict[str, Tool] = {},
) -> AsyncGenerator[Tuple[UpdateType, str], None]:
    """Yields partial tokens (ASSISTANT_TOKEN, partial_text) for streaming."""
    anthropic_client = anthropic.AsyncAnthropic()
    prompt = {
        "model": model,
        "max_tokens": 8192,
        "messages": [item for msg in messages for item in msg.anthropic_schema()],
        "system": system_message,
    }

    if tools or response_model:
        tool_schemas = [
            t.anthropic_schema(exclude_hidden=True) for t in (tools or {}).values()
        ]
        if response_model:
            tool_schemas.append(openai_schema(response_model).anthropic_schema)
            prompt["tool_choice"] = {"type": "tool", "name": response_model.__name__}
        prompt["tools"] = tool_schemas

    tool_calls = []

    async with anthropic_client.messages.stream(**prompt) as stream:
        async for chunk in stream:
            # Handle text deltas
            if (
                chunk.type == "content_block_delta"
                and chunk.delta
                and hasattr(chunk.delta, "text")
                and chunk.delta.text
            ):
                yield (UpdateType.ASSISTANT_TOKEN, chunk.delta.text)

            # Handle tool use
            elif chunk.type == "content_block_stop" and hasattr(chunk, "content_block"):
                if chunk.content_block.type == "tool_use":
                    tool_calls.append(ToolCall.from_anthropic(chunk.content_block))

            # Stop reason
            elif chunk.type == "message_delta" and hasattr(chunk.delta, "stop_reason"):
                yield (UpdateType.ASSISTANT_STOP, chunk.delta.stop_reason)

    # Return any accumulated tool calls at the end
    if tool_calls:
        for tool_call in tool_calls:
            yield (UpdateType.TOOL_CALL, tool_call)


async def async_openai_prompt(
    messages: List[Union[UserMessage, AssistantMessage]],
    system_message: Optional[str] = "You are a helpful assistant.",
    model: Literal[tuple(models)] = "gpt-4o-mini",
    response_model: Optional[type[BaseModel]] = None,
    tools: Dict[str, Tool] = {},
):
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY env is not set")

    messages_json = [item for msg in messages for item in msg.openai_schema()]
    if system_message:
        messages_json = [{"role": "system", "content": system_message}] + messages_json

    openai_client = openai.AsyncOpenAI()

    if response_model:
        response = await openai_client.beta.chat.completions.parse(
            model=model, messages=messages_json, response_format=response_model
        )
        return response.choices[0].message.parsed

    else:
        tools = (
            [t.openai_schema(exclude_hidden=True) for t in tools.values()]
            if tools
            else None
        )
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=messages_json, tools=tools
        )
        response = response.choices[0]
        content = response.message.content or ""
        tool_calls = [
            ToolCall.from_openai(t) for t in response.message.tool_calls or []
        ]
        stop = response.finish_reason == "stop"

        return content, tool_calls, stop


async def async_openai_prompt_stream(
    messages: List[Union[UserMessage, AssistantMessage]],
    system_message: Optional[str],
    model: Literal[tuple(models)] = "gpt-4o-mini",
    response_model: Optional[type[BaseModel]] = None,
    tools: Dict[str, Tool] = {},
) -> AsyncGenerator[Tuple[UpdateType, str], None]:
    """Yields partial tokens (ASSISTANT_TOKEN, partial_text) for streaming."""

    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY env is not set")

    messages_json = [item for msg in messages for item in msg.openai_schema()]
    if system_message:
        messages_json = [{"role": "system", "content": system_message}] + messages_json

    openai_client = openai.AsyncOpenAI()
    tools_schema = (
        [t.openai_schema(exclude_hidden=True) for t in tools.values()]
        if tools
        else None
    )

    if response_model:
        # Response models not supported in streaming mode for OpenAI
        raise NotImplementedError(
            "Response models not supported in streaming mode for OpenAI"
        )

    stream = await openai_client.chat.completions.create(
        model=model, messages=messages_json, tools=tools_schema, stream=True
    )

    tool_calls = []

    async for chunk in stream:
        delta = chunk.choices[0].delta

        # Handle text content
        if delta.content:
            yield (UpdateType.ASSISTANT_TOKEN, delta.content)

        # Handle tool calls
        if delta.tool_calls:
            for tool_call in delta.tool_calls:
                if tool_call.index is not None:
                    # Ensure we have a list long enough
                    while len(tool_calls) <= tool_call.index:
                        tool_calls.append(None)

                    if tool_calls[tool_call.index] is None:
                        tool_calls[tool_call.index] = ToolCall(
                            tool=tool_call.function.name, args={}
                        )

                    if tool_call.function.arguments:
                        current_args = tool_calls[tool_call.index].args
                        # Merge new arguments with existing ones
                        try:
                            new_args = json.loads(tool_call.function.arguments)
                            current_args.update(new_args)
                        except json.JSONDecodeError:
                            pass

        # Handle finish reason
        if chunk.choices[0].finish_reason:
            yield (UpdateType.ASSISTANT_STOP, chunk.choices[0].finish_reason)

    # Yield any accumulated tool calls at the end
    for tool_call in tool_calls:
        if tool_call:
            yield (UpdateType.TOOL_CALL, tool_call)


@retry(
    retry=retry_if_exception(
        lambda e: isinstance(e, (openai.RateLimitError, anthropic.RateLimitError))
    ),
    wait=wait_exponential(multiplier=5, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
@retry(
    retry=retry_if_exception(
        lambda e: isinstance(
            e,
            (
                openai.APIConnectionError,
                openai.InternalServerError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
            ),
        )
    ),
    wait=wait_exponential(multiplier=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def async_prompt(
    messages: List[Union[UserMessage, AssistantMessage]],
    system_message: Optional[str],
    model: Literal[tuple(models)] = "gpt-4o-mini",
    response_model: Optional[type[BaseModel]] = None,
    tools: Dict[str, Tool] = {},
) -> Tuple[str, List[ToolCall], bool]:
    """
    Non-streaming LLM call => returns (content, tool_calls, stop).
    """
    if model.startswith("claude"):
        # Use the non-stream Anthropics helper
        return await async_anthropic_prompt(
            messages, system_message, model, response_model, tools
        )
    else:
        # Use existing OpenAI path
        return await async_openai_prompt(
            messages, system_message, model, response_model, tools
        )


@retry(
    retry=retry_if_exception(
        lambda e: isinstance(e, (openai.RateLimitError, anthropic.RateLimitError))
    ),
    wait=wait_exponential(multiplier=5, max=60),
    stop=stop_after_attempt(3),
    reraise=True,
)
@retry(
    retry=retry_if_exception(
        lambda e: isinstance(
            e,
            (
                openai.APIConnectionError,
                openai.InternalServerError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
            ),
        )
    ),
    wait=wait_exponential(multiplier=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def async_prompt_stream(
    messages: List[Union[UserMessage, AssistantMessage]],
    system_message: Optional[str],
    model: str,
    response_model: Optional[type[BaseModel]] = None,
    tools: Optional[Dict[str, Tool]] = None,
) -> AsyncGenerator[Tuple[UpdateType, str], None]:
    """
    Streaming LLM call => yields (UpdateType.ASSISTANT_TOKEN, partial_text).
    Add a similar function for OpenAI if you need streaming from GPT-based models.
    """

    async_prompt_stream_method = (
        async_anthropic_prompt_stream
        if model.startswith("claude")
        else async_openai_prompt_stream
    )

    async for chunk in async_prompt_stream_method(
        messages, system_message, model, response_model, tools
    ):
        yield chunk


def anthropic_prompt(messages, system_message, model, response_model=None, tools=None):
    return asyncio.run(
        async_anthropic_prompt(messages, system_message, model, response_model, tools)
    )


def openai_prompt(messages, system_message, model, response_model=None, tools=None):
    return asyncio.run(
        async_openai_prompt(messages, system_message, model, response_model, tools)
    )


def prompt(messages, system_message, model, response_model=None, tools=None):
    return asyncio.run(
        async_prompt(messages, system_message, model, response_model, tools)
    )


# todo: `msg.error` not `msg.message.error`
class ThreadUpdate(BaseModel):
    type: UpdateType
    message: Optional[AssistantMessage] = None
    tool_name: Optional[str] = None
    tool_index: Optional[int] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    text: Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)



import instructor
from eve.models import Model
from eve.agent import Agent
from eve.mongo import get_collection

class SearchResult(BaseModel):
    """A matching result from the database search."""
    
    id: str = Field(..., description="The MongoDB ID of the result")
    name: str = Field(..., description="The name/title of the result")
    description: str = Field(..., description="A brief description of the result")
    relevance: str = Field(
        ..., 
        description="A brief explanation of why this result matches the search query"
    )

class SearchResults(BaseModel):
    """Results from searching the database."""
    
    results: List[SearchResult] = Field(
        ...,
        description="The matching results, ordered by relevance. Include only truly relevant results."
    )


search_template = """<mongodb_documents>
{{documents}}
</mongodb_documents>
<query>
{{query}}
</query>
<task>
Return a list of matching documents to the query.
</task>"""

agent_template = """<document>
  <_id>{{_id}}</_id>
  <name>{{name}}</name>
  <username>{{username}}</username>
  <description>{{description}}</description>
  <knowledge_description>{{knowledge_description}}</knowledge_description>
  <persona>{{persona[:750]}}</persona>
  <created_at>{{createdAt}}</created_at>
</document>"""

model_template = """<document>
  <_id>{{_id}}</_id>
  <name>{{name}}</name>
  <lora_model>{{lora_model}}</lora_model>
  <lora_trigger_text>{{lora_trigger_text}}</lora_trigger_text>
  <created_at>{{createdAt}}</created_at>
</document>"""

from jinja2 import Template
model_template = Template(model_template)
agent_template = Template(agent_template)
search_template = Template(search_template)

async def search_mongo(type: Literal["model", "agent"], query: str):
    """Search MongoDB for models or agents matching the query."""
    
    docs = []
    id_map = {}
    counter = 1
    
    if type == "model":
        collection = get_collection(Model.get_collection_name())
        for doc in collection.find({"base_model": "flux-dev", "public": True, "deleted": {"$ne": True}}):
            # Map the real ID to a counter
            id_map[counter] = str(doc["_id"])
            doc["_id"] = counter
            counter += 1
            docs.append(model_template.render(doc))

    elif type == "agent":
        collection = get_collection(Agent.collection_name)
        for doc in collection.find({"type": "agent", "public": True, "deleted": {"$ne": True}}):
            # Map the real ID to a counter
            id_map[counter] = str(doc["_id"])
            doc["_id"] = counter
            counter += 1
            docs.append(agent_template.render(doc))

    # Create context for LLM
    context = search_template.render(
        documents="\n".join(docs), 
        query=query
    )

    # Make LLM call
    system_message = f"""You are a search assistant that helps find relevant {type}s based on natural language queries. 
    Analyze the provided items and return only the most relevant matches for the query.
    Be selective - only return items that truly match the query's intent."""

    prompt = f"""<{type}s>
{context}
</{type}s>
<query>
"{query}"
</query>
<task>
Analyze these items and return only the ones that are truly relevant to this search query. 
Explain why each result matches the query criteria.
</task>"""

    print(context)



    # raise Exception("stop")

    client = instructor.from_openai(openai.AsyncOpenAI())
    results = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_message},
            {"role": "user", "content": prompt},
        ],
        response_model=SearchResults,
    )

    # Map the simple IDs back to real MongoDB IDs
    for result in results.results:
        result.id = id_map[int(result.id)]

    return results.results


async def async_think(
    agent: Agent,
    thread: Thread,
    user_message: UserMessage,
    force_reply: bool = True,
):
    # intention_description = "Response class to the last user message. Ignore if irrelevant, reply if relevant and you intend to say something."

    # if agent.reply_criteria:
    #     intention_description += (
    #         f"\nAdditional criteria for replying spontaneously: {agent.reply_criteria}"
    #     )

    class ChatThought(BaseModel):
        """A response to a chat message."""

        intention: Literal["ignore", "reply"] = Field(
            ..., description="Ignore if last message is irrelevant, reply if relevant or criteria met."
        )
        thought: str = Field(
            ...,
            description="A very brief thought about what relevance, if any, the last user message has to you, and a justification of your intention.",
        )
        tools: Optional[Literal[tuple(TOOL_CATEGORIES.keys())]] = Field(
            ...,
            description=f"Which tools to include in reply context",
        )
        recall_knowledge: bool = Field(
            ...,
            description="Whether to recall, refer to, or consult your knowledge base.",
        )

    # generate text blob of chat history
    chat = ""
    messages = thread.get_messages(25)
    for msg in messages:
        content = msg.content
        if msg.role == "user":
            if msg.attachments:
                content += f" (attachments: {msg.attachments})"
            name = "You" if msg.name == agent.name else msg.name or "User"
        elif msg.role == "assistant":
            name = agent.name
            for tc in msg.tool_calls:
                args = ", ".join([f"{k}={v}" for k, v in tc.args.items()])
                tc_result = dump_json(tc.result, exclude="blurhash")
                content += f"\n -> {tc.tool}({args}) -> {tc_result}"
        time_str = msg.createdAt.strftime("%H:%M")
        chat += f"<{name} {time_str}> {content}\n"

    # user message text
    content = user_message.content
    if user_message.attachments:
        content += f" (attachments: {user_message.attachments})"
    time_str = user_message.createdAt.strftime("%H:%M")
    message = f"<{user_message.name} {time_str}> {content}"

    if agent.knowledge:
        # if knowledge is requested but no knowledge description, create it now
        if not agent.knowledge_description:
            await refresh_agent(agent)
            agent.reload()

        knowledge_description = f"Summary: {agent.knowledge_description.summary}. Recall if: {agent.knowledge_description.retrieval_criteria}"
        knowledge_description = knowledge_think_template.render(
            knowledge_description=knowledge_description
        )
    else:
        knowledge_description = ""

    if agent.reply_criteria:
        reply_criteria = f"Note: You should additionally set reply to true if any of the follorwing criteria are met: {agent.reply_criteria}"
    else:
        reply_criteria = ""

    tool_descriptions = "\n".join([f"{k}: {v}" for k, v in TOOL_CATEGORIES.items()])
    tools_description = tools_template.render(
        tool_categories=tool_descriptions
    )

    prompt = thought_template.render(
        name=agent.name,
        chat=chat,
        tools_description=tools_description,
        knowledge_description=knowledge_description,
        message=message,
        reply_criteria=reply_criteria,
    )

    thought = await async_prompt(
        [UserMessage(content=prompt)],
        system_message=f"You analyze the chat on behalf of {agent.name} and generate a thought.",
        model="gpt-4o-mini",
        response_model=ChatThought,
    )

    if force_reply:
        thought.intention = "reply"

    return thought


def sentry_transaction(op: str, name: str):
    def decorator(func):
        @trace
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            transaction = start_transaction(op=op, name=name)
            try:
                async for item in func(*args, **kwargs):
                    yield item
            finally:
                transaction.finish()

        return wrapper

    return decorator


async def process_tool_call(
    thread: Thread,
    assistant_message: AssistantMessage,
    tool_call_index: int,
    tool_call: ToolCall,
    tools: Dict[str, Tool],
    user_id: str,
    agent_id: str,
) -> ThreadUpdate:
    """Process a single tool call and return the appropriate ThreadUpdate"""
    
    try:
        # get tool
        tool = tools.get(tool_call.tool)
        if not tool:
            raise Exception(f"Tool {tool_call.tool} not found.")

        # start task
        task = await tool.async_start_task(user_id, agent_id, tool_call.args)

        # update tool call with task id and status
        thread.update_tool_call(
            assistant_message.id,
            tool_call_index,
            {"task": ObjectId(task.id), "status": "pending"},
        )

        # wait for task to complete
        result = await tool.async_wait(task)
        thread.update_tool_call(assistant_message.id, tool_call_index, result)

        # task completed
        if result["status"] == "completed":
            # make a Creation
            name = task.args.get("prompt") or task.args.get("text_input")
            filename = result.get("output", [{}])[0].get("filename")
            media_attributes = result.get("output", [{}])[0].get("mediaAttributes")
            
            if filename and media_attributes:
                new_creation = Creation(
                    user=task.user,
                    requester=task.requester,
                    task=task.id,
                    tool=task.tool,
                    filename=filename,
                    mediaAttributes=media_attributes,
                    name=name,
                )
                new_creation.save()

            # yield update
            return ThreadUpdate(
                type=UpdateType.TOOL_COMPLETE,
                tool_name=tool_call.tool,
                tool_index=tool_call_index,
                result=result,
            )
        else:
            # yield error
            return ThreadUpdate(
                type=UpdateType.ERROR,
                tool_name=tool_call.tool,
                tool_index=tool_call_index,
                error=result.get("error"),
            )

    except Exception as e:
        # capture error
        capture_exception(e)
        traceback.print_exc()

        # update tool call with status and error
        thread.update_tool_call(
            assistant_message.id,
            tool_call_index,
            {"status": "failed", "error": str(e)},
        )

        # yield update
        return ThreadUpdate(
            type=UpdateType.ERROR,
            tool_name=tool_call.tool,
            tool_index=tool_call_index,
            error=str(e),
        )


@sentry_transaction(op="llm.prompt", name="async_prompt_thread")
async def async_prompt_thread(
    user: User,
    agent: Agent,
    thread: Thread,
    user_messages: Union[UserMessage, List[UserMessage]],
    tools: Dict[str, Tool],
    force_reply: bool = False,
    model: Literal[tuple(models)] = DEFAULT_MODEL,
    user_is_bot: bool = False,
    stream: bool = False,
):
    model = model or DEFAULT_MODEL
    user_messages = (
        user_messages if isinstance(user_messages, list) else [user_messages]
    )
    user_message_id = user_messages[-1].id

    # Rate limiting
    if USE_RATE_LIMITS:
        await RateLimiter.check_chat_rate_limit(user.id, None)

    # Apply bot-specific limits
    if user_is_bot:
        print("Bot message, stopping")
        return

    # thinking step
    if USE_THINKING:
        print("Thinking...")

        # a thought contains intention and tool pre-selection
        thought = await async_think(
            agent=agent,
            thread=thread,
            user_message=user_messages[-1],
            force_reply=force_reply,
        )
        thought = thought.model_dump()

    else:
        print("Skipping thinking, default to classic behavior")

        # Check mentions
        agent_mentioned = any(
            re.search(
                rf"\b{re.escape(agent.name.lower())}\b", (msg.content or "").lower()
            )
            for msg in user_messages
        )
        print("agent mentioned", agent_mentioned)

        # when there's no thinking, reply if mentioned or forced, and include all tools
        thought = {
            "thought": "none",
            "intention": "reply" if agent_mentioned or force_reply else "ignore",
            "tools": ["base"],
        }

    # for error tracing
    add_breadcrumb(
        category="prompt_thought",
        data={
            "user_message": user_messages[-1],
            "model": model,
            "thought": thought,
        },
    )

    # reply only if intention is "reply"
    should_reply = thought["intention"] == "reply"

    if should_reply:
        # update thread and continue
        thread.push({"messages": user_messages, "active": user_message_id})
    else:
        # update thread and stop
        thread.push({"messages": user_messages})
        return

    # yield start signal
    yield ThreadUpdate(type=UpdateType.START_PROMPT)

    while True:
        try:
            messages = thread.get_messages(25)

            # if creation tools are *not* requested, remove them from the tools list,
            # except for any that were already called in previous messages.
            if thought["tools"] == "base":
                include_tools = [
                    tc.tool
                    for msg in messages
                    if msg.role == "assistant"
                    for tc in msg.tool_calls
                ]  # start with tools already called
                include_tools.extend(BASE_TOOLS)  # add base tools
                tools = {k: v for k, v in tools.items() if k in include_tools}

            # if knowledge requested, prepend with full knowledge text
            if thought["recall_knowledge"] and agent.knowledge:
                knowledge = knowledge_reply_template.render(
                    knowledge=agent.knowledge
                )
            else:
                knowledge = ""

            system_message = system_template.render(
                name=agent.name, 
                persona=agent.persona, 
                knowledge=knowledge
            )

            # for error tracing
            add_breadcrumb(
                category="prompt_in",
                data={
                    "messages": messages,
                    "model": model,
                    "tools": (tools or {}).keys(),
                },
            )

            # main call to LLM, streaming
            if stream:
                content_chunks = []
                tool_calls = []
                stop = True

                async for update_type, content in async_prompt_stream(
                    messages,
                    system_message=system_message,
                    model=model,
                    tools=tools,
                ):
                    # stream an individual token
                    if update_type == UpdateType.ASSISTANT_TOKEN:
                        if not content:  # Skip empty content
                            continue
                        content_chunks.append(content)
                        yield ThreadUpdate(
                            type=UpdateType.ASSISTANT_TOKEN, text=content
                        )

                    # tool call
                    elif update_type == UpdateType.TOOL_CALL:
                        tool_calls.append(content)

                    # detect stop call
                    elif update_type == UpdateType.ASSISTANT_STOP:
                        stop = content == "end_turn" or content == "stop"

                # Create assistant message from accumulated content
                content = "".join(content_chunks)

            # main call to LLM, non-streaming
            else:
                content, tool_calls, stop = await async_prompt(
                    messages,
                    system_message=system_message,
                    model=model,
                    tools=tools,
                )

            # for error tracing
            add_breadcrumb(
                category="prompt_out",
                data={"content": content, "tool_calls": tool_calls, "stop": stop},
            )

            # create assistant message
            assistant_message = AssistantMessage(
                content=content or "",
                tool_calls=tool_calls,
                reply_to=user_messages[-1].id,
            )

            # push assistant message to thread and pop user message from actives array
            pushes = {"messages": assistant_message}
            pops = {"active": user_message_id} if stop else {}
            thread.push(pushes, pops)
            assistant_message = thread.messages[-1]

            # yield update
            yield ThreadUpdate(
                type=UpdateType.ASSISTANT_MESSAGE, message=assistant_message
            )

        except Exception as e:
            # capture error
            capture_exception(e)
            traceback.print_exc()

            # create assistant message
            assistant_message = AssistantMessage(
                content="I'm sorry, but something went wrong internally. Please try again later.",
                reply_to=user_messages[-1].id,
            )

            # push assistant message to thread and pop user message from actives array
            pushes = {"messages": assistant_message}
            pops = {"active": user_message_id}
            thread.push(pushes, pops)

            # yield error message
            yield ThreadUpdate(
                type=UpdateType.ERROR, message=assistant_message, error=str(e)
            )

            # stop thread
            stop = True
            break

        # handle tool calls in batches of 4
        tool_calls = assistant_message.tool_calls or []
        for b in range(0, len(tool_calls), 4):
            batch = enumerate(tool_calls[b:b + 4])
            tasks = [
                process_tool_call(
                    thread,
                    assistant_message,
                    b + idx,
                    tool_call,
                    tools,
                    user.id,
                    agent.id
                )
                for idx, tool_call in batch
            ]
            
            # wait for batch to complete and yield each result
            results = await asyncio.gather(*tasks, return_exceptions=False)
            for result in results:
                yield result

        # if stop called, break out of loop
        if stop:
            break

    yield ThreadUpdate(type=UpdateType.END_PROMPT)


def prompt_thread(
    user: User,
    agent: Agent,
    thread: Thread,
    user_messages: Union[UserMessage, List[UserMessage]],
    tools: Dict[str, Tool],
    force_reply: bool = False,
    model: Literal[tuple(models)] = DEFAULT_MODEL,
    user_is_bot: bool = False,
):
    async_gen = async_prompt_thread(
        user, agent, thread, user_messages, tools, force_reply, model, user_is_bot
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while True:
            try:
                yield loop.run_until_complete(async_gen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        loop.close()


async def async_title_thread(thread: Thread, *extra_messages: UserMessage):
    """
    Generate a title for a thread
    """

    class TitleResponse(BaseModel):
        """A title for a thread of chat messages. It must entice a user to click on the thread when they are interested in the subject."""

        title: str = Field(
            description="a phrase of 2-5 words (or up to 30 characters) that conveys the subject of the chat thread. It should be concise and terse, and not include any special characters or punctuation."
        )

    system_message = "You are an expert at creating concise titles for chat threads."
    messages = thread.get_messages()
    messages.extend(extra_messages)
    messages.append(UserMessage(content="Come up with a title for this thread."))

    try:
        result = await async_prompt(
            messages,
            system_message=system_message,
            model="gpt-4o-mini",
            response_model=TitleResponse,
        )
        thread.title = result.title
        thread.save()

    except Exception as e:
        capture_exception(e)
        traceback.print_exc()
        return


def title_thread(thread: Thread, *extra_messages: UserMessage):
    return asyncio.run(async_title_thread(thread, *extra_messages))
