import os
from typing import Any, Callable, List

from ai_prompter import Prompter
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from loguru import logger
from typing_extensions import TypedDict

from open_notebook.ai.provision import provision_langchain_model
from open_notebook.domain.notebook import Source
from open_notebook.domain.transformation import DefaultPrompts, Transformation
from open_notebook.exceptions import (
    IncompleteGenerationError,
    InvalidInputError,
    OpenNotebookError,
)
from open_notebook.utils import clean_thinking_content, partials
from open_notebook.utils.error_classifier import classify_error
from open_notebook.utils.long_document import (
    assumed_context_tokens,
    budget_message,
    budget_within_context,
    chunk_budget,
    split_for_context,
)
from open_notebook.utils.text_utils import extract_text_content
from open_notebook.utils.token_utils import token_count


class TransformationState(TypedDict):
    input_text: str
    source: Source
    transformation: Transformation
    output: str


def _map_output_tokens() -> int:
    """How many tokens a *part* of a long document may write.

    Small on purpose. A part is an intermediate, not the answer: the reduce
    step writes the real one. Too generous and the map step costs as much as the
    whole transformation, repeated once per part - and, more to the point, a
    part that does not finish is a part that is thrown away. The measurement
    that sets the default is on DEFAULT_MAP_OUTPUT_TOKENS.
    """
    raw = os.getenv(
        "OPEN_NOTEBOOK_TRANSFORMATION_MAP_TOKENS", str(DEFAULT_MAP_OUTPUT_TOKENS)
    )
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_MAP_OUTPUT_TOKENS
    except ValueError:
        return DEFAULT_MAP_OUTPUT_TOKENS


def _answer_output_tokens() -> int:
    """How many tokens the final answer may write.

    Generous, because the answer is the thing the reader gets. What it
    actually writes is decided by the transformation's instructions - a dense
    summary is a few hundred tokens - so the real limit is the room left in
    the model's window once the document has been read, which is computed
    where the document is known.
    """
    raw = os.getenv("OPEN_NOTEBOOK_TRANSFORMATION_ANSWER_TOKENS", "8192")
    try:
        value = int(raw)
        return value if value > 0 else 8192
    except ValueError:
        return 8192


#: Tokens held back for the provider's own framing of a request - the chat
#: template it wraps the messages in, and the stop tokens it reserves.
SAFETY_TOKENS = 256

#: The smallest answer worth leaving room for.
#:
#: A part of a long document only has to write a short analysis, but a
#: document that fits in one pass has to write the whole answer, and the room
#: left for it is what the split budget has to keep clear. 1024 tokens is
#: roughly a full paragraph of dense summary - more than the insights this
#: produces, and comfortably more than nothing.
MIN_ANSWER_RESERVE = 1024

#: How much a *part* of a long document may write, when nothing says otherwise.
#:
#: The number that decides whether a long document can be analysed at all on
#: the hardware this supports. A part is an intermediate: it is analysed, kept,
#: and combined with the others, and it has to *finish* to be kept - a part that
#: is interrupted is a part whose work is thrown away, and the parts are what
#: the resume is made of.
#:
#: Measured on the floor this supports - a phone, qwen2.5-1.5b at two threads -
#: 250 tokens of generation is about 152 s, and 500 is about twice that on top
#: of the prompt evaluation every part pays anyway. At 500 a part outlives the
#: interruptions such a device hands out, so no part ever completed to be kept.
#: DEFAULT_LLM_TIMEOUT_SECONDS in open_notebook.config is the matching timeout,
#: which 500 tokens of writing would have outlasted too.
DEFAULT_MAP_OUTPUT_TOKENS = 250


def _context_tokens(chain: Any) -> int:
    """The model's context window, or the floor assumed for one that hides it.

    `num_ctx` is the field every adapter that has a window uses, and it is the
    one that decides how large a part may be.

    A provider that does not expose it is not a provider without a window: it
    is a provider that did not say, and the window is the fact the whole
    splitting path is built on. Returning nothing here let the configured
    budget through unchallenged *and* switched off the reduce's batching, which
    is how a phone serving through llama.cpp came to have every part of a long
    document rejected against its 4,096-token window, with the one guard that
    would have caught it silent about it. So the floor is assumed - see
    ASSUMED_CONTEXT_TOKENS for the measurement and what it costs.
    """
    value = getattr(chain, "num_ctx", None)
    try:
        reported = int(value) if value else 0
    except (TypeError, ValueError):
        reported = 0
    return reported if reported > 0 else assumed_context_tokens()


def _without_reasoning(chain: Any) -> None:
    """Turn the model's reasoning off, for a call that does not need it.

    The map step is mechanical: analyse this part of the document, in this
    shape. The reasoning a thinking model does about it is not part of the
    answer - and at the map step's small output budget it is *all* there is.
    The model thinks, the budget runs out, and the part comes back empty.
    Measured: five hundred tokens of reasoning and no analysis at all.

    It is a field on the model rather than a request parameter, because the
    adapter builds the request from the model's own settings and passes
    nothing else through.
    """
    if hasattr(chain, "reasoning"):
        chain.reasoning = False


def _fit_batches(partials: List[str], limit: int) -> List[List[str]]:
    """Group the partials into batches whose combined text fits `limit` tokens.

    A batch that is over the limit would have its combine prompt truncated by
    the provider, so the last partials in it would be lost - which is the same
    silent loss this whole path exists to avoid, one level up.
    """
    batches: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0
    for partial in partials:
        tokens = token_count(partial)
        if current and current_tokens + tokens > limit:
            batches.append(current)
            current = [partial]
            current_tokens = tokens
        else:
            current.append(partial)
            current_tokens += tokens
    if current:
        batches.append(current)
    return batches


async def _reduce_to_fit(
    provision: Callable[[int], Any],
    state: dict,
    instructions: str,
    partials: List[str],
    context_tokens: int,
    answer_tokens: int,
) -> List[str]:
    """Reduce the partials until the ones left can be combined in one pass.

    The partial analyses of a long document can add up to more than the
    model's window - twenty-four parts at five hundred tokens each is more than
    an 8k window takes - and a combine prompt that does not fit is truncated
    by the provider, which would drop the last parts and leave an answer that
    claims to cover the document while covering most of it.

    So the partials are combined in batches that do fit, and the results of
    those are combined again, until a single pass remains.

    The window is always a number by the time it gets here. It used to be
    optional, and an absent one returned the partials unchanged - so on a
    provider that did not advertise a window this batching never ran, and the
    one pass it skipped was the one that truncated. _context_tokens assumes
    the floor instead, for exactly this reason.

    The reduction stops short of losing anything: if the partials cannot be
    made to fit, they are returned as they are and the loss is reported.
    """
    if len(partials) <= 1:
        return partials

    overhead = token_count(
        Prompter(prompt_template="transformation/combine").render(
            data={**state, "instructions": instructions, "partials": ""}
        )
    )
    limit = max(1, context_tokens - overhead - SAFETY_TOKENS - MIN_ANSWER_RESERVE)

    while True:
        joined = "\n\n---\n\n".join(partials)
        needed = overhead + token_count(joined) + SAFETY_TOKENS + MIN_ANSWER_RESERVE
        if needed <= context_tokens:
            return partials

        batches = _fit_batches(partials, limit)
        if len(batches) <= 1 or len(batches) >= len(partials):
            # Either there is nothing smaller to reduce to, or every batch holds
            # a single partial - so reducing again would not make progress.
            # Combining anyway is better than not answering, and the loss is
            # reported rather than hidden.
            logger.warning(
                f"the {len(partials)} partial analyses do not fit the model's "
                f"window of {context_tokens} tokens and cannot be reduced "
                f"further, so the last of them may be lost"
            )
            return partials

        reduced: List[str] = []
        for batch in batches:
            prompt = Prompter(prompt_template="transformation/combine").render(
                data={
                    **state,
                    "instructions": instructions,
                    "partials": "\n\n---\n\n".join(batch),
                }
            )
            chain = await provision(answer_tokens)
            response = await chain.ainvoke(
                [SystemMessage(content=prompt), HumanMessage(content="")]
            )
            reduced.append(extract_text_content(response.content))
        logger.info(
            f"reduced {len(partials)} partial analyses to {len(reduced)} to fit "
            f"the model's window"
        )
        partials = reduced


async def run_transformation(state: dict, config: RunnableConfig) -> dict:
    source_obj = state.get("source")
    source: Source = source_obj if isinstance(source_obj, Source) else None  # type: ignore[assignment]
    content = state.get("input_text")
    assert source or content, "No content to transform"
    transformation: Transformation = state["transformation"]

    try:
        if not content:
            content = source.full_text
        if not content or not str(content).strip():
            # A source whose extraction produced no text would hand the model
            # an empty input, and whatever it invents would be saved as an
            # insight.
            raise InvalidInputError("There is no text content to transform")
        # transformation.prompt is user-controlled free text. Never compile it as
        # Jinja template *source* (Prompter(template_text=...)) - pass it as a
        # plain render variable into a fixed, developer-authored template instead.
        # See docs/7-DEVELOPMENT/security.md (GHSA-f35w-wx37-26q7).
        instructions = transformation.prompt
        default_prompts: DefaultPrompts = DefaultPrompts(
            transformation_instructions=None
        )
        if default_prompts.transformation_instructions:
            instructions = (
                f"{default_prompts.transformation_instructions}\n\n{instructions}"
            )

        system_prompt = Prompter(prompt_template="transformation/execute").render(
            data={**state, "instructions": instructions}
        )
        # The part instructions are rendered once, without a part, and the part
        # itself travels as the user message.
        #
        # Putting the part into the instructions as well would send it twice,
        # and a part can run to thousands of tokens - so the provider would
        # truncate the pair and the model would analyse only the start of the
        # part it was given. The two templates therefore have to agree on this:
        # `execute.jinja` has always put the document in the user message, and
        # `execute_part.jinja` now does the same.
        part_prompt = Prompter(prompt_template="transformation/execute_part").render(
            data={**state, "instructions": instructions}
        )
        content_str = str(content) if content else ""
        # Whether the parts of a long document can be kept between runs. A
        # transformation with no source - a measurement, say - has nowhere to
        # keep them.
        resumable = source is not None and bool(getattr(transformation, "id", None))

        from open_notebook.utils.job_control import (
            get_current_command_id,
            report_progress_record,
        )

        cmd_id = get_current_command_id()
        await report_progress_record(cmd_id, progress=30, stage="loading_model")

        model_id = config.get("configurable", {}).get("model_id")
        answer_tokens = _answer_output_tokens()
        map_tokens = _map_output_tokens()

        async def provision(max_output_tokens: int) -> Any:
            return await provision_langchain_model(
                str([SystemMessage(content=system_prompt)]),
                model_id,
                "transformation",
                max_tokens=max_output_tokens,
            )

        # The model is provisioned before the document is split, because the
        # window it was given decides how large a part may be. Splitting first
        # and hoping the parts fit is how a part ends up truncated by the
        # provider, silently, with the answer still presented as covering the
        # whole document.
        #
        # The selection payload is the whole document rather than a part, so
        # that a document long enough to warrant a larger model still gets one
        # - the same rule the transformation used before it was split.
        chain = await provision_langchain_model(
            str(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=content_str),
                ]
            ),
            model_id,
            "transformation",
            max_tokens=answer_tokens,
        )
        await report_progress_record(cmd_id, progress=50, stage="model_ready")

        context_tokens = _context_tokens(chain)
        # What a part does not get to use: the instructions in front of it, the
        # answer written after it, and a margin for the provider's framing.
        reserved = (
            token_count(part_prompt)
            + max(map_tokens, MIN_ANSWER_RESERVE)
            + SAFETY_TOKENS
        )
        budget = budget_within_context(context_tokens, reserved)
        wanted = chunk_budget()
        message = budget_message(context_tokens, budget, wanted, reserved)
        if message:
            logger.warning(message)

        # A document longer than the context is split, analysed in parts, and
        # the parts combined. See open_notebook/utils/long_document.py for why
        # this is not optional: the provider truncates silently, and the insight
        # is then presented as covering the whole document when it covers only
        # the first few thousand tokens of it.
        chunks = split_for_context(content_str, budget_tokens=budget)
        if len(chunks) > 1:
            logger.info(
                f"source is {token_count(content_str)} tokens, which does not fit "
                f"the context: analysing in {len(chunks)} parts and combining"
            )

        await report_progress_record(cmd_id, progress=60, stage="generating")

        if len(chunks) > 1:
            # Map: one pass per part, each producing a *short* analysis.
            #
            # The short output budget is the whole economics of this. Generation
            # on this hardware runs at about 1.1 tokens per second while
            # reading a prompt is nearly free, so a part that wrote a full-length
            # summary would cost as much as the final answer - eighteen parts of
            # seven hundred tokens each is three and a half hours for one
            # source. A part that writes a couple of hundred tokens is minutes.
            #
            # The parts are then combined into one answer of the length the
            # transformation actually calls for, by the reduce step below.
            map_chain = await provision(map_tokens)
            _without_reasoning(map_chain)

            # A long transformation is a long series of generation calls, and
            # an interruption in the middle of it - the device restarting, the
            # battery giving out - would throw all of it away. So the parts
            # that are done are kept, and a restarted transformation picks up
            # where it left off.
            resume: dict[int, str] = {}
            # The parts are the document cut up, so the resume is only valid
            # for the same cut: the same number of parts, made the same way.
            split_key = f"{len(chunks)}:{budget}:{context_tokens}"
            if resumable:
                resume = await partials.load(
                    str(source.id), str(transformation.id), content_str, split_key
                )
                if resume:
                    logger.info(
                        f"resuming: {len(resume)} of {len(chunks)} parts were "
                        f"already analysed"
                    )

            partials_list: List[str] = []
            for index, chunk in enumerate(chunks):
                await report_progress_record(
                    cmd_id,
                    progress=60 + int(25 * index / len(chunks)),
                    stage=f"analysing_part_{index + 1}_of_{len(chunks)}",
                )
                if index in resume:
                    partials_list.append(resume[index])
                    logger.info(f"part {index + 1}/{len(chunks)} was already analysed")
                    continue
                part = await map_chain.ainvoke(
                    [
                        SystemMessage(content=part_prompt),
                        HumanMessage(content=chunk),
                    ]
                )
                # `.content`, not the message itself.
                #
                # `extract_text_content` handles a plain string and a list of
                # content blocks; a message is neither, so it falls through to
                # `str(message)` - which is the message's *repr*. The partial
                # would then reach the combine step wrapped in
                # "content='...' additional_kwargs={} response_metadata={}", with
                # its newlines escaped, and every one of those characters would
                # be spending the combine step's input budget.
                text = extract_text_content(part.content)
                if not text.strip():
                    # The model spent its whole budget thinking and had nothing
                    # left to say. The reasoning is better than nothing, and
                    # saying so is better than a hole in the coverage that
                    # nothing reports.
                    reasoning = (getattr(part, "additional_kwargs", {}) or {}).get(
                        "reasoning_content", ""
                    )
                    if reasoning.strip():
                        logger.warning(
                            f"part {index + 1}/{len(chunks)} produced no analysis, "
                            f"only reasoning; the reasoning is being used as the "
                            f"analysis"
                        )
                        text = reasoning
                    else:
                        raise IncompleteGenerationError(
                            f"The model returned nothing for part {index + 1} of "
                            f"{len(chunks)} of the document."
                        )
                partials_list.append(text)
                logger.info(
                    f"part {index + 1}/{len(chunks)} analysed: "
                    f"{len(partials_list[-1])} characters"
                )
                if resumable:
                    await partials.store(
                        str(source.id),
                        str(transformation.id),
                        content_str,
                        split_key,
                        index,
                        text,
                    )
            # Reduce: one pass over the partials, or several if they do not fit.
            await report_progress_record(cmd_id, progress=88, stage="combining_parts")
            partials_list = await _reduce_to_fit(
                provision,
                state,
                instructions,
                partials_list,
                context_tokens,
                answer_tokens,
            )
            combine_prompt = Prompter(prompt_template="transformation/combine").render(
                data={
                    **state,
                    "instructions": instructions,
                    "partials": "\n\n---\n\n".join(partials_list),
                }
            )
            # The answer gets the room that is left after the partials have
            # been read. Asking for more would not give it more: the provider
            # would stop the generation at the window, and the answer would be
            # cut off mid-sentence.
            room = answer_tokens
            if context_tokens:
                room = max(
                    MIN_ANSWER_RESERVE,
                    min(
                        answer_tokens,
                        context_tokens - token_count(combine_prompt) - SAFETY_TOKENS,
                    ),
                )
            combine_chain = await provision(room)
            response = await combine_chain.ainvoke(
                [
                    SystemMessage(content=combine_prompt),
                    HumanMessage(content=""),
                ]
            )
        else:
            # The whole document fits, so it is read in one pass - and the
            # answer gets whatever room is left after it.
            room = answer_tokens
            if context_tokens:
                room = max(
                    MIN_ANSWER_RESERVE,
                    min(
                        answer_tokens,
                        context_tokens
                        - token_count(system_prompt)
                        - token_count(content_str)
                        - SAFETY_TOKENS,
                    ),
                )
            single_chain = await provision(room)
            response = await single_chain.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=content_str),
                ]
            )

        # Provider adapters use different keys for the termination reason.
        # Check before saving: a nonempty answer can still be incomplete.
        metadata = response.response_metadata
        for key in ("finish_reason", "stop_reason", "done_reason"):
            reason = metadata.get(key)
            if isinstance(reason, str) and reason.lower() in ("length", "max_tokens"):
                raise IncompleteGenerationError(
                    "The model reached its generation limit before completing the "
                    "transformation. Try a shorter transformation or a different model."
                )

        await report_progress_record(cmd_id, progress=80, stage="postprocessing")
        # Clean thinking content from the response
        response_content = extract_text_content(response.content)
        cleaned_content = clean_thinking_content(response_content)

        if not cleaned_content.strip():
            raise IncompleteGenerationError(
                "The model returned no usable text for the transformation after "
                "removing thinking content. Try a shorter transformation or a "
                "different model."
            )

        if source:
            await source.add_insight(transformation.title, cleaned_content)
            # The answer is written, so the parts it was made from are no
            # longer needed. If they were kept, a later transformation of the
            # same source would resume onto them.
            if resumable:
                await partials.clear(str(source.id), str(transformation.id))

        await report_progress_record(cmd_id, progress=95, stage="insight_saved")

        return {
            "output": cleaned_content,
        }
    except OpenNotebookError:
        raise
    except Exception as e:
        error_class, user_message = classify_error(e)
        raise error_class(user_message) from e


agent_state = StateGraph(TransformationState)
agent_state.add_node("agent", run_transformation)  # type: ignore[type-var]
agent_state.add_edge(START, "agent")
agent_state.add_edge("agent", END)
graph = agent_state.compile()
